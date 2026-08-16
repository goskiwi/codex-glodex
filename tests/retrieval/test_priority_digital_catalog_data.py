from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def _products() -> list[dict[str, object]]:
    root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (root / "data/digital-interview-v1/products.json").read_text(encoding="utf-8")
    )
    return payload["products"]


def test_priority_categories_have_model_first_coverage() -> None:
    targets = {
        "phone": 70,
        "cpu": 40,
        "gpu": 40,
        "camera": 45,
        "headphones": 30,
        "monitor": 35,
        "tablet": 30,
        "smartwatch": 30,
        "ssd": 30,
        "router": 30,
        "nas": 30,
        "speaker": 30,
        "printer": 30,
        "external_drive": 30,
        "desktop": 30,
        "mini_pc": 30,
        "memory": 30,
        "keyboard": 30,
        "mouse": 30,
        "television": 30,
        "game_console": 30,
        "handheld_console": 30,
        "game_controller": 30,
        "microphone": 30,
        "projector": 30,
        "webcam": 30,
        "ebook_reader": 30,
        "fitness_tracker": 30,
        "action_camera": 30,
        "motherboard": 30,
        "hdd": 30,
        "power_supply": 30,
        "cooling": 30,
        "pc_case": 30,
        "camera_lens": 30,
        "mesh_wifi": 30,
        "wifi_adapter": 30,
        "usb_flash_drive": 30,
        "memory_card": 30,
    }
    models: dict[str, set[str]] = {category: set() for category in targets}
    for product in _products():
        category = product["category"]
        if category not in targets:
            continue
        attributes = product["attributes"]
        models[category].add(attributes["model"].casefold())

    assert all(len(models[category]) >= target for category, target in targets.items())


def test_priority_expansion_has_unique_ids_and_safe_sources() -> None:
    products = _products()
    identifiers = [product["product_id"] for product in products]
    duplicates = {value: count for value, count in Counter(identifiers).items() if count > 1}

    assert duplicates == {}
    assert all(
        product["specification_source"]["url"].startswith("https://") for product in products
    )
    assert all(
        product["price_source"]["source_type"] != "MANUFACTURER_LIST_PRICE"
        for product in products
        if product["product_id"].startswith("curated-v2-")
    )


def test_core_category_curated_models_have_category_evidence() -> None:
    required = {
        "phone": {"processor", "screen", "refresh_rate", "connectivity"},
        "cpu": {"socket", "cores_threads", "architecture", "integrated_graphics"},
        "gpu": {"architecture", "vram", "outputs"},
        "camera": {"sensor", "mount", "autofocus", "video"},
        "headphones": {"form_factor", "connection", "noise_cancelling", "microphone"},
        "monitor": {"panel_type", "resolution", "refresh_rate", "connectivity"},
    }
    products = [
        product
        for product in _products()
        if product["product_id"].startswith("curated-v2-") and product["category"] in required
    ]

    assert products
    for product in products:
        attributes = product["attributes"]
        assert required[product["category"]] <= attributes.keys(), product["product_id"]


def test_pc_build_curated_models_have_compatibility_specs() -> None:
    required = {
        "motherboard": {"socket", "chipset", "form_factor", "memory_type", "expansion"},
        "hdd": {"capacity", "form_factor", "interface", "spindle_speed", "workload"},
        "power_supply": {"wattage", "form_factor", "atx_standard", "efficiency", "gpu_connector"},
        "cooling": {"cooler_type", "radiator_size", "socket_support", "fan_size", "tdp_class"},
        "pc_case": {
            "case_size",
            "motherboard_support",
            "gpu_clearance",
            "cooler_clearance",
            "radiator_support",
        },
    }
    products = [
        product
        for product in _products()
        if product["product_id"].startswith("curated-v2-") and product["category"] in required
    ]

    assert products
    for product in products:
        attributes = product["attributes"]
        assert required[product["category"]] <= attributes.keys(), product["product_id"]
        assert all(attributes[name].strip() for name in required[product["category"]])
        assert all(attributes[name].strip() for name in required[product["category"]])


def test_third_wave_curated_models_have_decision_specs() -> None:
    required = {
        "desktop": {"processor", "gpu", "ram", "storage", "form_factor"},
        "mini_pc": {"processor", "gpu", "ram", "storage", "connectivity"},
        "memory": {"capacity", "memory_type", "speed", "latency", "voltage"},
        "keyboard": {"layout", "switch_type", "connection", "polling_rate"},
        "mouse": {"sensor", "dpi", "polling_rate", "weight", "connection"},
        "television": {"screen_size", "resolution", "panel_type", "refresh_rate", "hdr"},
    }
    products = [
        product
        for product in _products()
        if product["product_id"].startswith("curated-v2-") and product["category"] in required
    ]

    assert products
    for product in products:
        attributes = product["attributes"]
        assert required[product["category"]] <= attributes.keys(), product["product_id"]
