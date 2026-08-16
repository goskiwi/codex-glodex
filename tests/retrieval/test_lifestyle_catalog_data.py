from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "data/digital-interview-v1/products.json"
KNOWLEDGE = ROOT / "data/digital-interview-v1/category-knowledge.json"

REQUIRED = {
    "air_conditioner": {"rated_capacity", "energy_efficiency", "applicable_area", "noise"},
    "refrigerator": {"capacity", "dimensions", "cooling_type", "compartments"},
    "washing_machine": {"wash_capacity", "drying", "motor", "hygiene"},
    "robot_vacuum": {"suction_power", "navigation", "obstacle_avoidance", "base_station"},
    "vacuum_cleaner": {"suction_power", "battery_life", "filtration", "brush_heads"},
    "air_purifier": {"cadr", "applicable_area", "filter", "sensors"},
    "water_purifier": {"flow_rate", "membrane", "wastewater_ratio", "filter_life"},
    "rice_cooker": {"capacity", "heating", "inner_pot", "reservation"},
    "coffee_machine": {"machine_type", "pump_pressure", "grinder", "milk_system"},
    "electric_toothbrush": {
        "vibration_frequency",
        "cleaning_modes",
        "pressure_sensor",
        "battery_life",
    },
    "dishwasher": {"place_settings", "installation", "drying", "wash_programs"},
    "water_heater": {"heater_type", "capacity", "temperature_control", "safety"},
    "microwave": {"capacity", "microwave_power", "cooking_modes", "inverter"},
    "air_fryer": {"capacity", "heating_power", "temperature_range", "heating_system"},
    "induction_cooker": {"rated_power", "burners", "power_levels", "cookware"},
    "electric_kettle": {"capacity", "rated_power", "temperature_control", "inner_material"},
    "hair_dryer": {"motor_speed", "air_speed", "temperature_control", "ions"},
    "electric_fan": {"fan_type", "motor", "air_distance", "noise"},
    "humidifier": {"humidification_type", "humidification_rate", "tank_capacity", "noise"},
    "dehumidifier": {
        "dehumidification_capacity",
        "applicable_area",
        "tank_capacity",
        "humidity_control",
    },
    "oven": {"capacity", "temperature_range", "heating_system", "temperature_control"},
    "steam_oven": {"capacity", "steam_modes", "temperature_range", "water_tank"},
    "range_hood": {"airflow", "static_pressure", "hood_style", "noise"},
    "gas_stove": {"heat_load", "burners", "thermal_efficiency", "safety"},
    "electric_pressure_cooker": {"capacity", "pressure", "heating", "inner_pot"},
    "blender": {"capacity", "motor_power", "motor_speed", "cleaning"},
    "juicer": {"juicing_type", "feed_chute", "juice_yield", "rotation_speed"},
    "garment_steamer": {"steam_output", "steam_pressure", "heat_up_time", "water_tank"},
    "electric_shaver": {"shaving_system", "blade_count", "wet_dry", "battery_life"},
    "massage_gun": {"amplitude", "stall_force", "speed_levels", "percussions"},
    "floor_washer": {"suction_power", "roller_system", "self_cleaning", "battery_life"},
    "mite_vacuum": {"suction_power", "tapping_frequency", "uv_sterilization", "filtration"},
    "smart_lock": {"unlock_methods", "biometrics", "lock_cylinder", "security"},
    "security_camera": {"resolution", "night_vision", "tracking", "storage"},
    "dash_cam": {"resolution", "image_sensor", "parking_monitoring", "channels"},
    "drone": {"camera", "flight_time", "obstacle_avoidance", "transmission_range"},
    "power_bank": {"battery_capacity", "output_power", "ports", "fast_charge_protocols"},
    "charger": {"output_power", "ports", "gan", "fast_charge_protocols"},
    "dock": {"port_count", "host_interface", "display_output", "power_delivery"},
    "smart_scale": {"measurement_metrics", "electrodes", "weight_accuracy", "connectivity"},
    "oral_irrigator": {
        "pressure_range",
        "pressure_modes",
        "tank_capacity",
        "nozzles",
        "battery_life",
    },
    "hair_clipper": {
        "cutting_length",
        "blade_material",
        "speed_modes",
        "waterproof",
        "battery_life",
    },
    "blood_pressure_monitor": {
        "measurement_site",
        "cuff_range",
        "accuracy",
        "arrhythmia_detection",
        "memory_users",
    },
    "thermometer": {
        "measurement_method",
        "measurement_time",
        "accuracy",
        "fever_alert",
        "memory_records",
    },
    "desk_lamp": {
        "illuminance",
        "color_rendering",
        "color_temperature",
        "dimming",
        "flicker_control",
    },
    "steam_iron": {
        "rated_power",
        "steam_output",
        "soleplate",
        "temperature_control",
        "water_tank",
    },
    "bread_maker": {"capacity", "programs", "heating", "reservation", "dispenser"},
    "toaster": {"slots", "browning_levels", "slot_width", "functions", "crumb_tray"},
    "food_processor": {
        "bowl_capacity",
        "rated_power",
        "attachments",
        "speed_modes",
        "safety",
    },
    "electric_lunch_box": {
        "capacity",
        "rated_power",
        "heating_method",
        "containers",
        "reservation",
    },
}


def _catalog() -> dict[str, object]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_lifestyle_categories_have_thirty_distinct_models_and_decision_specs() -> None:
    products = _catalog()["products"]
    for category, required in REQUIRED.items():
        selected = [product for product in products if product["category"] == category]
        models = {
            (
                product["attributes"]["brand"].casefold(),
                product["attributes"]["model"].casefold(),
            )
            for product in selected
        }

        assert len(selected) >= 30, category
        assert len(models) >= 30, category
        for product in selected:
            attributes = product["attributes"]
            assert required <= attributes.keys(), product["product_id"]
            assert all(attributes[field].strip() for field in required)
            assert product["specification_source"]["url"].startswith("https://")
            assert product["reference_price_cny"] != "0.00"


def test_lifestyle_categories_are_in_category_knowledge() -> None:
    payload = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
    categories = {item["category_id"]: item for item in payload["categories"]}

    for category in REQUIRED:
        item = categories[f"electronics.{category}"]
        assert item["source_count"] >= 30
        assert len(item["price_tiers"]) == 3
        assert len(item["evidence_refs"]) >= 3
