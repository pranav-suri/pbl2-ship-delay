# app.py
import flask
from flask import Flask, request, jsonify
import joblib
import pandas as pd
import numpy as np
import os
import time
from datetime import datetime
import requests  # For making API calls to Open-Meteo
import json  # For handling JSON responses
from typing import Dict, List, Union, Optional, Any  # Type hints
from weather_cache import weather_cache  # Import our weather cache

# --- TensorFlow / Keras Imports ---
# Import specific layers if needed for custom objects, otherwise load_model is usually sufficient
import tensorflow as tf
from tensorflow import keras # type: ignore[import]

# Import the helper functions
# We might need a new helper for sequence prep or do it inline
from utils import calculate_weather_features_from_forecast

# Import the time series forecasting functionality
from time_series import (
    ARIMAForecaster,
    create_arima_endpoint,
    prepare_daily_delay_time_series,
)

# --- Configuration ---
MODEL_DIR = "models"
KERAS_MODEL_DIR = "models_keras" # Separate directory for Keras models
# !!! IMPORTANT: Define the exact feature names your models expect !!!
# These must match the lists used during training (after preprocessing name generation for one-hot)
# The preprocessor inside the pipeline handles the actual encoding/scaling
# We just need the input DataFrame columns for the pipeline's preprocessor step
EXPECTED_CATEGORICAL_FEATURES = [
    "type",
    "arrival_hour",
    "arrival_dayofweek",
    "arrival_month",
]
EXPECTED_NUMERICAL_FEATURES_BASE = ["teu"]

# Port operational features that might be missing from frontend requests
PORT_OPERATIONAL_FEATURES = [
    "berth_occupancy_rate_at_pred_time",
    "num_at_berth_at_pred_time",
    "num_waiting_entry_at_pred_time",
    "num_waiting_berth_at_pred_time",
]

# Add port operational features to base numerical features
EXPECTED_NUMERICAL_FEATURES_BASE += PORT_OPERATIONAL_FEATURES

# Add weather feature names generated by the util function (derive dynamically or list explicitly)
# Example (Derive names - safer if util changes):
_temp_windows = [6, 12, 24, 48]
_temp_weather_vars = ["wind_speed_knots", "visibility_nm", "wave_height_m"]
_temp_states = ["Fog", "HighWind"]
WEATHER_FEATURE_NAMES = []
for w in _temp_windows:
    for var in _temp_weather_vars:
        for agg in ["mean", "max", "min", "std"]:
            WEATHER_FEATURE_NAMES.append(f"{var}_{agg}_{w}h")
    for state in _temp_states:
        WEATHER_FEATURE_NAMES.append(f"{state}_hours_{w}h")
EXPECTED_NUMERICAL_FEATURES = EXPECTED_NUMERICAL_FEATURES_BASE + WEATHER_FEATURE_NAMES
# --- End of Configuration ---

# --- Weather Code Dictionary ---
WEATHER_CODE_DESCRIPTION = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Heavy drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

# Simplified weather codes for layman terms
WEATHER_CODE_SIMPLE = {
    0: "clear",
    1: "clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "foggy",
    51: "light rain",
    53: "light rain",
    55: "rain",
    56: "freezing rain",
    57: "freezing rain",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}

# --- Sequence Model Specific Configuration ---
# Must match training!
WEATHER_SEQUENCE_VARS = ['wind_speed_knots', 'visibility_nm', 'wave_height_m', 'precipitation_mmhr'] # As used in training
SEQUENCE_LENGTH = 48 # As used in training

# Combine static features expected by RNN/CNN (assuming they use the same static set)
STATIC_NUMERICAL_FEATURES_RNN_CNN = EXPECTED_NUMERICAL_FEATURES_BASE # TEU + Port State
STATIC_CATEGORICAL_FEATURES_RNN_CNN = EXPECTED_CATEGORICAL_FEATURES # Type + Time Features
ALL_STATIC_FEATURES_RNN_CNN = STATIC_CATEGORICAL_FEATURES_RNN_CNN + STATIC_NUMERICAL_FEATURES_RNN_CNN

# --- Load Port Operational Defaults ---
port_operational_defaults_by_hour = {}
DEFAULT_PORT_OPERATIONAL_VALUES = {}

# Try to load the training data to calculate realistic defaults
try:
    # Check if synthetic data exists - you may need to adjust the path
    synthetic_data_path = "synthetic_operations_log.csv"
    if os.path.exists(synthetic_data_path):
        print(
            f"Loading synthetic data from {synthetic_data_path} to calculate port operational defaults..."
        )

        # Load the data
        df = pd.read_csv(synthetic_data_path)

        # If timestamp column exists, extract hour
        if "timestamp" in df.columns:
            # Convert to datetime if it's not already
            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                df["timestamp"] = pd.to_datetime(df["timestamp"])

            # Extract hour
            df["hour"] = df["timestamp"].dt.hour

            # Calculate average values for each port operational feature by hour
            for hour in range(24):
                hour_data = df[df["hour"] == hour]
                hour_defaults = {}

                for feature in PORT_OPERATIONAL_FEATURES:
                    if feature in df.columns:
                        hour_defaults[feature] = hour_data[feature].mean()
                    else:
                        # Default values if feature not in data
                        if "rate" in feature:
                            hour_defaults[feature] = 0.7  # 70% default occupancy rate
                        else:
                            hour_defaults[feature] = 5.0  # Default count of 5 vessels

                port_operational_defaults_by_hour[hour] = hour_defaults

            print("Successfully calculated port operational defaults by hour.")
        else:
            raise ValueError("Timestamp column not found in synthetic data")
    else:
        raise FileNotFoundError(f"Synthetic data file {synthetic_data_path} not found")

except Exception as e:
    print(f"Could not calculate port operational defaults from data: {e}")
    print("Using hard-coded default values instead.")

    # Use hard-coded defaults by hour if data loading fails
    for hour in range(24):
        # Vary defaults slightly by hour to capture daily patterns
        multiplier = 1.0 + 0.2 * np.sin(hour * np.pi / 12)  # Peaks at midday

        port_operational_defaults_by_hour[hour] = {
            "berth_occupancy_rate_at_pred_time": 0.7 * multiplier,  # 70% base occupancy
            "num_at_berth_at_pred_time": 5.0 * multiplier,  # 5 vessels at berth
            "num_waiting_entry_at_pred_time": 3.0
            * multiplier,  # 3 vessels waiting entry
            "num_waiting_berth_at_pred_time": 2.0
            * multiplier,  # 2 vessels waiting berth
        }
    print("Created default port operational values by hour.")

# Default values without hour differentiation as fallback
DEFAULT_PORT_OPERATIONAL_VALUES = {
    "berth_occupancy_rate_at_pred_time": 0.7,  # 70% occupancy
    "num_at_berth_at_pred_time": 5.0,  # 5 vessels at berth
    "num_waiting_entry_at_pred_time": 3.0,  # 3 vessels waiting entry
    "num_waiting_berth_at_pred_time": 2.0,  # 2 vessels waiting berth
}

# --- Load Models & Preprocessors On Startup ---
models = {}
keras_models = {}
fitted_preprocessor = None
fitted_static_preprocessor_rnn_cnn = None
fitted_sequence_scaler = None

print("Loading models and preprocessors...")

# Load scikit-learn pipelines
try:
    for filename in os.listdir(MODEL_DIR):
        if filename.endswith(".joblib"):
            # --- Load Preprocessors ---
            if filename == "main_preprocessor.joblib":
                try:
                    fitted_preprocessor = joblib.load(os.path.join(MODEL_DIR, filename))
                    print("Loaded main_preprocessor.joblib successfully.")
                except Exception as e:
                    print(f"Error loading main_preprocessor.joblib: {e}")
                continue # Don't add preprocessor to models dict

            if filename == "static_preprocessor_rnn_cnn.joblib":
                try:
                    fitted_static_preprocessor_rnn_cnn = joblib.load(os.path.join(MODEL_DIR, filename))
                    print("Loaded static_preprocessor_rnn_cnn.joblib successfully.")
                except Exception as e:
                    print(f"Error loading static_preprocessor_rnn_cnn.joblib: {e}")
                continue

            if filename == "sequence_scaler.joblib":
                try:
                    fitted_sequence_scaler = joblib.load(os.path.join(MODEL_DIR, filename))
                    print("Loaded sequence_scaler.joblib successfully.")
                except Exception as e:
                    print(f"Error loading sequence_scaler.joblib: {e}")
                continue

            # --- Load sklearn Models ---
            if filename.endswith("_pipeline.joblib"): # Only load pipelines
                model_name = filename.replace("_pipeline.joblib", "").replace("_", " ").title()
                model_path = os.path.join(MODEL_DIR, filename)
                print(f"Loading sklearn pipeline: {model_name} from {model_path}")
                try:
                    models[model_name] = joblib.load(model_path)
                    print(f"Loaded {model_name} successfully.")
                except Exception as load_error:
                    print(f"Error loading sklearn pipeline {model_name} from {model_path}: {load_error}")

except FileNotFoundError:
    print(f"Warning: Model directory '{MODEL_DIR}' not found for sklearn models/preprocessors.")
except Exception as e:
    print(f"An unexpected error occurred during sklearn model/preprocessor loading: {e}")

# Load Keras models
try:
    if not os.path.exists(KERAS_MODEL_DIR):
        print(f"Warning: Keras model directory '{KERAS_MODEL_DIR}' not found.")
    else:
        for filename in os.listdir(KERAS_MODEL_DIR):
             if filename.endswith(".keras"): # Load models saved in the recommended Keras format
                 model_name = filename.replace("_best.keras", "").replace("_", " ").upper() # Example naming convention
                 model_path = os.path.join(KERAS_MODEL_DIR, filename)
                 print(f"Loading Keras model: {model_name} from {model_path}")
                 try:
                     keras_models[model_name] = keras.models.load_model(model_path)
                     print(f"Loaded {model_name} successfully.")
                 except Exception as load_error:
                     print(f"Error loading Keras model {model_name} from {model_path}: {load_error}")
except Exception as e:
    print(f"An unexpected error occurred during Keras model loading: {e}")


# --- Check if essential components were loaded ---
if not models and not keras_models:
    print("CRITICAL WARNING: No models (sklearn or Keras) were loaded.")
if fitted_preprocessor is None:
    print("CRITICAL WARNING: Main preprocessor ('main_preprocessor.joblib') not loaded. MLP and potentially other models will fail.")
if fitted_static_preprocessor_rnn_cnn is None or fitted_sequence_scaler is None:
    print("CRITICAL WARNING: Preprocessors/scalers for RNN/CNN not loaded. These models will fail.")


# --- Combine model dictionaries for endpoint listing ---
all_available_models = list(models.keys()) + list(keras_models.keys())


# --- Create Flask App ---
app = Flask(__name__)

# --- Initialize ARIMA Forecaster ---
try:
    # Try to load the ARIMA forecaster if statsmodels is available
    arima_forecaster = ARIMAForecaster(model_dir=MODEL_DIR)
    # Try to load any pre-trained ARIMA models
    arima_model_loaded = arima_forecaster.load_model()
    sarima_model_loaded = arima_forecaster.load_model(seasonal=True)

    if arima_model_loaded:
        models["ARIMA"] = "ARIMA time series forecaster"
    if sarima_model_loaded:
        models["SARIMA"] = "Seasonal ARIMA time series forecaster"

    # If there's no pre-trained model yet, we'll still provide the endpoint
    # The models will be trained when needed or loaded after training
    if not (arima_model_loaded or sarima_model_loaded):
        print("No pre-trained ARIMA models found. They will be trained when needed.")

    # Add ARIMA forecaster to models dictionary for reference
    models["arima_forecaster"] = arima_forecaster

except ImportError:
    print("statsmodels not available - ARIMA forecasting will be disabled")
except Exception as e:
    print(f"Error initializing ARIMA forecaster: {e}")

# Register ARIMA endpoints
create_arima_endpoint(app, models)


# --- API Endpoints ---
@app.route("/", methods=["GET"])
def health_check():
    """Basic health check endpoint."""
    return (
        jsonify(
            {
                "status": "OK",
                "message": "Prediction server is running.",
                "available_models": all_available_models,
            }
        ),
        200,
    )

@app.route("/predict", methods=["POST"])
def predict():
    """Endpoint to make delay predictions."""
    start_time = time.time()

    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()

    # --- 1. Input Validation ---
    required_fields = [
        "vessel_type",
        "teu",
        "arrival_timestamp_str",
        "hourly_weather_forecast",
        "model_name",
    ]
    if not all(field in data for field in required_fields):
        return (
            jsonify({"error": f"Missing required fields. Required: {required_fields}"}),
            400,
        )

    model_name = data["model_name"]
    is_keras_model = model_name in keras_models
    is_sklearn_model = model_name in models

    if not is_keras_model and not is_sklearn_model:
        return (
            jsonify({"error": f"Model '{model_name}' not found. Available: {all_available_models}"}),
            404,
        )

    # --- 2. Feature Preparation (Common Part) ---
    try:
        vessel_type = data["vessel_type"]
        teu = float(data["teu"])
        arrival_ts_str = data["arrival_timestamp_str"]
        try:
            arrival_ts = pd.to_datetime(arrival_ts_str)
        except ValueError:
            return jsonify({"error": "Invalid format for 'arrival_timestamp_str'. Use ISO 8601."}), 400

        arrival_hour = arrival_ts.hour
        arrival_dayofweek = arrival_ts.dayofweek
        arrival_month = arrival_ts.month
        hourly_forecast = data["hourly_weather_forecast"] # List of dicts

        # --- 3. Model-Specific Input Preparation & Prediction ---
        prediction_raw = None

        # === A) Scikit-learn Pipeline Models ===
        if is_sklearn_model:
            if model_name.upper() in ["ARIMA", "SARIMA"]: # Handle ARIMA separately if needed
                return (
                    jsonify(
                        {
                            "error": "ARIMA/SARIMA prediction not implemented via this endpoint yet."
                        }
                    ),
                    400,
                )  # Or call your ARIMA logic

            print(f"Preparing input for sklearn model: {model_name}")
            selected_pipeline = models[model_name]

            # Calculate weather summary features
            weather_features = calculate_weather_features_from_forecast(arrival_ts, hourly_forecast)
            if weather_features.empty or weather_features.isnull().all():
                return jsonify({"error": "Could not calculate weather summary features."}), 400

            # Assemble input dictionary
            input_data = {
                "type": [vessel_type], "teu": [teu], "arrival_hour": [arrival_hour],
                "arrival_dayofweek": [arrival_dayofweek], "arrival_month": [arrival_month],
            }
            # Add port defaults
            for feature in PORT_OPERATIONAL_FEATURES:
                default_value = port_operational_defaults_by_hour.get(arrival_hour, DEFAULT_PORT_OPERATIONAL_VALUES).get(feature, 0.0) # Safer get
                input_data[feature] = [float(data.get(feature, default_value))] # Use provided or default
            # Add weather features
            for feature_name in WEATHER_FEATURE_NAMES:
                input_data[feature_name] = [weather_features.get(feature_name, 0.0)] # Use get with default 0

            # Create DataFrame expected by the pipeline's preprocessor
            all_expected_sklearn_features = EXPECTED_CATEGORICAL_FEATURES + EXPECTED_NUMERICAL_FEATURES
            try:
                input_df = pd.DataFrame(input_data, columns=all_expected_sklearn_features)
                # Ensure dtypes (optional but good practice)
                input_df[EXPECTED_NUMERICAL_FEATURES] = input_df[EXPECTED_NUMERICAL_FEATURES].astype(float)
                input_df[EXPECTED_CATEGORICAL_FEATURES] = input_df[EXPECTED_CATEGORICAL_FEATURES].astype(str)
            except KeyError as e:
                return jsonify({"error": f"Internal error: Missing expected feature column for sklearn: {e}"}), 500

            # Predict using the sklearn pipeline
            prediction_raw = selected_pipeline.predict(input_df)

        # === B) Keras Models ===
        elif is_keras_model:
            print(f"Preparing input for Keras model: {model_name}")
            selected_keras_model = keras_models[model_name]

            # --- MLP Input Prep ---
            if model_name == "MLP": # Check model name convention
                if fitted_preprocessor is None:
                    return (
                        jsonify(
                            {
                                "error": "Internal error: Main preprocessor not loaded for MLP."
                            }
                        ),
                        500,
                    )

                # Calculate weather summary features (same as sklearn)
                weather_features = calculate_weather_features_from_forecast(arrival_ts, hourly_forecast)
                if weather_features.empty or weather_features.isnull().all():
                    return jsonify({"error": "Could not calculate weather summary features for MLP."}), 400

                # Assemble input dictionary (same as sklearn)
                input_data = {
                    "type": [vessel_type], "teu": [teu], "arrival_hour": [arrival_hour],
                    "arrival_dayofweek": [arrival_dayofweek], "arrival_month": [arrival_month],
                }
                for feature in PORT_OPERATIONAL_FEATURES:
                    default_value = port_operational_defaults_by_hour.get(arrival_hour, DEFAULT_PORT_OPERATIONAL_VALUES).get(feature, 0.0)
                    input_data[feature] = [float(data.get(feature, default_value))]
                for feature_name in WEATHER_FEATURE_NAMES:
                    input_data[feature_name] = [weather_features.get(feature_name, 0.0)]

                # Create DataFrame
                all_expected_mlp_features = EXPECTED_CATEGORICAL_FEATURES + EXPECTED_NUMERICAL_FEATURES
                try:
                    input_df = pd.DataFrame(input_data, columns=all_expected_mlp_features)
                    input_df[EXPECTED_NUMERICAL_FEATURES] = input_df[EXPECTED_NUMERICAL_FEATURES].astype(float)
                    input_df[EXPECTED_CATEGORICAL_FEATURES] = input_df[EXPECTED_CATEGORICAL_FEATURES].astype(str)
                except KeyError as e:
                    return jsonify({"error": f"Internal error: Missing expected feature column for MLP: {e}"}), 500

                # Preprocess using the loaded preprocessor
                input_processed = fitted_preprocessor.transform(input_df)
                if hasattr(input_processed, "toarray"): input_processed = input_processed.toarray() # Densify
                input_processed = input_processed.astype(np.float32) # Keras often prefers float32

                # Predict using Keras model
                prediction_raw = selected_keras_model.predict(input_processed)

            # --- RNN / CNN Input Prep ---
            elif model_name in ["RNN LSTM", "CNN 1D"]: # Adjust names if needed
                if fitted_static_preprocessor_rnn_cnn is None or fitted_sequence_scaler is None:
                    return (
                        jsonify(
                            {
                                "error": f"Internal error: Preprocessors/scaler for {model_name} not loaded."
                            }
                        ),
                        500,
                    )

                # --- Calculate Weather Summary Features (Needed for the Static Preprocessor) ---
                # This is the same calculation used for sklearn models and MLP
                weather_features = calculate_weather_features_from_forecast(arrival_ts, hourly_forecast)
                if weather_features.empty or weather_features.isnull().all():
                    return jsonify({"error": f"Could not calculate weather summary features for {model_name}."}), 400

                # 1. Prepare Static Features Input DataFrame
                static_input_data = {
                    "type": [vessel_type], "teu": [teu], "arrival_hour": [arrival_hour],
                    "arrival_dayofweek": [arrival_dayofweek], "arrival_month": [arrival_month],
                }
                # Add port defaults
                for feature in PORT_OPERATIONAL_FEATURES:
                    default_value = port_operational_defaults_by_hour.get(arrival_hour, DEFAULT_PORT_OPERATIONAL_VALUES).get(feature, 0.0)
                    static_input_data[feature] = [float(data.get(feature, default_value))]

                # --- *** ADD WEATHER SUMMARY FEATURES TO STATIC INPUT DATA *** ---
                for feature_name in WEATHER_FEATURE_NAMES:
                    static_input_data[feature_name] = [weather_features.get(feature_name, 0.0)] # Use get with default 0
                # --- *** END OF ADDITION *** ---

                # Define the complete list of columns the static preprocessor expects
                # This should match exactly how it was trained
                all_expected_static_features = (
                    STATIC_CATEGORICAL_FEATURES_RNN_CNN + STATIC_NUMERICAL_FEATURES_RNN_CNN + WEATHER_FEATURE_NAMES
                 )
                # Check if the definition during training was different - adjust if necessary
                # For example, if only base numerical + categoricals were used for the static branch,
                # then WEATHER_FEATURE_NAMES should NOT be added here, and the preprocessor
                # loaded should also reflect that structure.
                # HOWEVER, the error message implies the preprocessor DOES expect them.

                try:
                    # Create DataFrame with all expected columns
                    static_df = pd.DataFrame(static_input_data, columns=all_expected_static_features)
                    # Ensure dtypes
                    num_features_for_static_df = STATIC_NUMERICAL_FEATURES_RNN_CNN + WEATHER_FEATURE_NAMES
                    static_df[num_features_for_static_df] = static_df[num_features_for_static_df].astype(float)
                    static_df[STATIC_CATEGORICAL_FEATURES_RNN_CNN] = static_df[STATIC_CATEGORICAL_FEATURES_RNN_CNN].astype(str)

                except KeyError as e:
                    return (
                        jsonify(
                            {
                                "error": f"Internal error: Missing static feature column for {model_name}: {e}"
                            }
                        ),
                        500,
                    )

                # Preprocess static features using the *correct* preprocessor
                static_input_processed = fitted_static_preprocessor_rnn_cnn.transform(static_df)
                if hasattr(static_input_processed, "toarray"): static_input_processed = static_input_processed.toarray()
                static_input_processed = static_input_processed.astype(np.float32)

                # 2. Prepare Sequence Input
                try:
                    # Create DataFrame from forecast list
                    forecast_df = pd.DataFrame(hourly_forecast)
                    if "timestamp" not in forecast_df.columns: raise ValueError("'timestamp' missing")
                    forecast_df["timestamp"] = pd.to_datetime(forecast_df["timestamp"])
                    forecast_df = forecast_df.set_index("timestamp").sort_index()

                    # Extract sequence data
                    forecast_start_time = arrival_ts.floor('h') # Align with training prep
                    forecast_end_time = forecast_start_time + pd.Timedelta(hours=SEQUENCE_LENGTH)

                    # Select relevant columns and time range
                    seq_data = forecast_df.loc[forecast_start_time : forecast_end_time, WEATHER_SEQUENCE_VARS]

                    if len(seq_data) < SEQUENCE_LENGTH:
                        # Handle insufficient forecast data (e.g., padding or error)
                        # For now, error out if not exact length
                        return jsonify({"error": f"Insufficient hourly forecast data provided. Need {SEQUENCE_LENGTH} hours from {forecast_start_time}. Found {len(seq_data)}."}), 400

                    sequence_input_raw = seq_data.values # Shape: (SEQUENCE_LENGTH, num_weather_vars)
                    sequence_input_raw = sequence_input_raw.astype(np.float32)

                    # Scale sequence data using loaded scaler
                    # Reshape for scaler -> scale -> reshape back to 3D for model (add batch dim)
                    sequence_input_scaled = fitted_sequence_scaler.transform(sequence_input_raw)
                    sequence_input_final = sequence_input_scaled.reshape(1, SEQUENCE_LENGTH, len(WEATHER_SEQUENCE_VARS)) # Add batch dimension

                except Exception as seq_e:
                    print(f"Error preparing sequence input: {seq_e}")
                    import traceback; traceback.print_exc()
                    return jsonify({"error": f"Internal error processing weather sequence: {seq_e}"}), 500

                # 3. Predict using RNN/CNN model (expects list/tuple of inputs)
                prediction_raw = selected_keras_model.predict([sequence_input_final, static_input_processed])

            else:
                # Should not happen if initial check is correct
                return (
                    jsonify(
                        {
                            "error": f"Internal error: Unknown Keras model type '{model_name}'."
                        }
                    ),
                    500,
                )

        # --- 4. Post-process Prediction ---
        if prediction_raw is None:
            # Handle cases where prediction didn't happen (e.g., ARIMA error above)
            return (
                jsonify(
                    {
                        "error": "Prediction could not be generated for the selected model."
                    }
                ),
                500,
            )

        predicted_delay = max(0, float(prediction_raw.flatten()[0])) # Flatten Keras output, extract, ensure >= 0

    except ValueError as ve:
        return jsonify({"error": f"Invalid input data value: {ve}"}), 400
    except Exception as e:
        print(f"An error occurred during prediction processing: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": "An internal server error occurred during prediction."}), 500

    # --- 5. Format Response ---
    end_time = time.time()
    response = {
        "model_used": model_name,
        "input_data_summary": {
            "vessel_type": vessel_type, "teu": teu,
            "arrival_timestamp": arrival_ts.isoformat(),
            "port_defaults_used": [f for f in PORT_OPERATIONAL_FEATURES if f not in data],
        },
        "predicted_total_weather_delay_hrs": round(predicted_delay, 4),
        "processing_time_seconds": round(end_time - start_time, 4),
    }

    return jsonify(response), 200


@app.route("/weather", methods=["GET"])
def get_weather():
    """
    Endpoint to fetch weather data from Open-Meteo API with caching.

    Query parameters:
    - lat: latitude (required)
    - lon: longitude (required)
    - forecast: Whether to return a forecast (default: 'true') or current weather ('false')

    Returns weather data including temperature, humidity, precipitation,
    weather code (in layman terms), wind speed, wind direction, visibility, and wave height.
    """
    # Get parameters
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    forecast_param = request.args.get("forecast", "true").lower()

    # Cache status for debugging/monitoring
    cache_status = "miss"

    # Validate required parameters
    if not lat or not lon:
        return (
            jsonify(
                {"error": "Missing required parameters: lat and lon must be provided"}
            ),
            400,
        )

    try:
        # Convert to float to validate
        lat = float(lat)
        lon = float(lon)

        # Check coordinate ranges
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return (
                jsonify(
                    {
                        "error": "Invalid coordinates: latitude must be between -90 and 90, longitude between -180 and 180"
                    }
                ),
                400,
            )
    except ValueError as e:
        print(f"ValueError: {e}")
        return (
            jsonify(
                {"error": "Invalid coordinates: lat and lon must be numeric values"}
            ),
            400,
        )

    # Determine if we want forecast or current weather
    is_forecast = forecast_param in ("true", "t", "yes", "y", "1")

    # Try to get data from cache first
    if is_forecast:
        cached_data = weather_cache.get_forecast_weather(lat, lon)
        if cached_data:
            # Add cache status to response
            cached_data["cache_status"] = "hit_forecast"
            return jsonify(cached_data), 200
    else:
        cached_data = weather_cache.get_current_weather(lat, lon)
        if cached_data:
            # Add cache status to response
            cached_data["cache_status"] = "hit_current"
            return jsonify(cached_data), 200

    # Cache miss, need to fetch from API
    # Build API request
    base_url = "https://api.open-meteo.com/v1/forecast"

    # Common parameters for both current and forecast
    weather_params = {
        "latitude": lat,
        "longitude": lon,
        "temperature_unit": "celsius",
        "windspeed_unit": "kn",  # Knots to match the existing app logic
        "timezone": "GMT",
    }

    # Add appropriate parameters based on request type
    if is_forecast:
        # For forecast data (48 hours)
        weather_params.update(
            {
                "forecast_days": 3,  # 48 hours
                "hourly": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "visibility",
                ],
            }
        )
    else:
        # For current weather data
        weather_params.update(
            {
                "current": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "visibility",
                ]
            }
        )

    # Get weather data from Open-Meteo
    try:
        response = requests.get(base_url, params=weather_params)
        response.raise_for_status()  # Raise exception for HTTP errors
        weather_data = response.json()

        # Now get marine data for wave height
        marine_url = "https://marine-api.open-meteo.com/v1/marine"
        marine_params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "GMT",
        }

        if is_forecast:
            marine_params.update(
                {"forecast_days": 3, "hourly": ["wave_height"]}  # 48 hours
            )
        else:
            marine_params.update({"current": ["wave_height"]})

        marine_response = requests.get(marine_url, params=marine_params)
        marine_response.raise_for_status()
        marine_data = marine_response.json()

        # Process and combine the data
        result = {
            "coordinates": {"latitude": lat, "longitude": lon},
            "units": {
                "temperature": "°C",
                "humidity": "%",
                "precipitation": "mm",
                "wind_speed": "knots",
                "wind_direction": "degrees",
                "visibility": "meters",
                "wave_height": "meters",
            },
            "cache_status": "miss",  # Indicate this was a cache miss
        }

        if is_forecast:
            # Process forecast data
            hourly_data = []

            for i in range(len(weather_data.get("hourly", {}).get("time", []))):
                time_entry = weather_data["hourly"]["time"][i]
                weather_code_val = weather_data["hourly"]["weather_code"][i]

                # Create entry for this hour
                hour_entry = {
                    "timestamp": time_entry,
                    "temperature": weather_data["hourly"]["temperature_2m"][i],
                    "humidity": weather_data["hourly"]["relative_humidity_2m"][i],
                    "precipitation": weather_data["hourly"]["precipitation"][i],
                    "weather_code": weather_code_val,
                    "weather_description": WEATHER_CODE_DESCRIPTION.get(
                        weather_code_val, "Unknown"
                    ),
                    "weather_simple": WEATHER_CODE_SIMPLE.get(
                        weather_code_val, "unknown"
                    ),
                    "wind_speed": weather_data["hourly"]["wind_speed_10m"][i],
                    "wind_direction": weather_data["hourly"]["wind_direction_10m"][i],
                    "visibility": weather_data["hourly"]["visibility"][i],
                    "wave_height": (
                        marine_data.get("hourly", {}).get("wave_height", [])[i]
                        if i < len(marine_data.get("hourly", {}).get("wave_height", []))
                        else 0.0
                    ),
                }
                hourly_data.append(hour_entry)

            result["forecast"] = hourly_data
            result["forecast_hours"] = len(hourly_data)

            # Store in cache
            weather_cache.cache_forecast_weather(lat, lon, result)
        else:
            # Process current weather data
            weather_code_val = weather_data["current"]["weather_code"]

            result["current"] = {
                "timestamp": weather_data["current"]["time"],
                "temperature": weather_data["current"]["temperature_2m"],
                "humidity": weather_data["current"]["relative_humidity_2m"],
                "precipitation": weather_data["current"]["precipitation"],
                "weather_code": weather_code_val,
                "weather_description": WEATHER_CODE_DESCRIPTION.get(
                    weather_code_val, "Unknown"
                ),
                "weather_simple": WEATHER_CODE_SIMPLE.get(weather_code_val, "unknown"),
                "wind_speed": weather_data["current"]["wind_speed_10m"],
                "wind_direction": weather_data["current"]["wind_direction_10m"],
                "visibility": weather_data["current"]["visibility"],
                "wave_height": marine_data.get("current", {}).get("wave_height", 0.0),
            }

            # Store in cache
            weather_cache.cache_current_weather(lat, lon, result)

        return jsonify(result), 200

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Error fetching weather data: {str(e)}"}), 500
    except KeyError as e:
        return (
            jsonify({"error": f"Error processing weather data: Missing key {str(e)}"}),
            500,
        )
    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route("/weather/cache/stats", methods=["GET"])
def get_weather_cache_stats():
    """
    Endpoint to get statistics about the weather cache.
    Useful for monitoring cache performance and diagnosing issues.
    """
    stats = weather_cache.get_stats()
    return (
        jsonify(
            {
                "cache_stats": stats,
                "config": {
                    "current_weather_ttl_seconds": weather_cache.CURRENT_WEATHER_TTL,
                    "forecast_weather_ttl_seconds": weather_cache.FORECAST_WEATHER_TTL,
                    "coordinate_precision": weather_cache.COORD_PRECISION,
                    "cleanup_interval_seconds": weather_cache.CLEANUP_INTERVAL,
                },
            }
        ),
        200,
    )


# --- Run the App ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True) # Use debug=False for production
