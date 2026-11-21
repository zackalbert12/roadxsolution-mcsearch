"""Commodity filter: equipment -> commodities mapping (verified with Zack)
and SAFER cargo-keyword matching.

Selecting a commodity lists the MCs of carriers whose trucks haul it.
SAFER does not record truck type, so a commodity matches a carrier when
the carrier's SAFER cargo list contains the cargo keywords that the
equipment hauling that commodity typically carries.
"""

# commodity -> {"equipment": [...], "keywords": [SAFER cargo names]}
COMMODITIES = {
    "General Freight": {
        "equipment": ["Box Truck", "Dry Van"],
        "keywords": ["General Freight"],
    },
    "Pallets": {
        "equipment": ["Box Truck", "Non-CDL Hotshot", "Dry Van"],
        "keywords": ["General Freight"],
    },
    "Furniture": {
        "equipment": ["Box Truck"],
        "keywords": ["Household Goods", "General Freight"],
    },
    "Appliances": {
        "equipment": ["Box Truck"],
        "keywords": ["General Freight"],
    },
    "Retail Goods": {
        "equipment": ["Box Truck", "Dry Van"],
        "keywords": ["General Freight"],
    },
    "Household Goods": {
        "equipment": ["Box Truck"],
        "keywords": ["Household Goods"],
    },
    "Expedited Loads": {
        "equipment": ["Box Truck"],
        "keywords": ["General Freight"],
    },
    "Building Material": {
        "equipment": ["Non-CDL Hotshot", "CDL Hotshot", "Flatbed"],
        "keywords": ["Building Materials", "Logs, Poles, Beams, Lumber"],
    },
    "Auto Parts": {
        "equipment": ["Non-CDL Hotshot", "Cargo Van", "Sprinter Van"],
        "keywords": ["General Freight"],
    },
    "Small Machinery": {
        "equipment": ["Non-CDL Hotshot"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Cars (1-2 cars)": {
        "equipment": ["Non-CDL Hotshot"],
        "keywords": ["Motor Vehicles", "Drive/Tow away"],
    },
    "Farm Equipment": {
        "equipment": ["Non-CDL Hotshot"],
        "keywords": ["Agricultural/Farm Supplies", "Machinery, Large Objects"],
    },
    "Machinery": {
        "equipment": ["CDL Hotshot", "Flatbed", "Step Deck"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Cars (2-3 cars)": {
        "equipment": ["CDL Hotshot"],
        "keywords": ["Motor Vehicles", "Drive/Tow away"],
    },
    "Construction Equipment": {
        "equipment": ["CDL Hotshot", "Flatbed", "Lowboy"],
        "keywords": ["Construction", "Building Materials", "Machinery, Large Objects"],
    },
    "Generators": {
        "equipment": ["CDL Hotshot"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Oilfield Equipment": {
        "equipment": ["CDL Hotshot"],
        "keywords": ["Oilfield Equipment"],
    },
    "Partial Flatbed Loads": {
        "equipment": ["CDL Hotshot", "Flatbed"],
        "keywords": ["Building Materials", "Metal: sheets, coils, rolls"],
    },
    "Parcels": {
        "equipment": ["Cargo Van"],
        "keywords": ["General Freight"],
    },
    "Documents": {
        "equipment": ["Cargo Van"],
        "keywords": ["General Freight"],
    },
    "Medical Supplies": {
        "equipment": ["Cargo Van"],
        "keywords": ["General Freight"],
    },
    "One Small Pallet": {
        "equipment": ["Cargo Van"],
        "keywords": ["General Freight"],
    },
    "Expedited Freight": {
        "equipment": ["Sprinter Van"],
        "keywords": ["General Freight"],
    },
    "Pharma": {
        "equipment": ["Sprinter Van", "Reefer"],
        "keywords": ["Refrigerated Food", "General Freight"],
    },
    "Electronics": {
        "equipment": ["Sprinter Van", "Dry Van"],
        "keywords": ["General Freight"],
    },
    "2-3 Pallets": {
        "equipment": ["Sprinter Van"],
        "keywords": ["General Freight"],
    },
    "Produce": {
        "equipment": ["Reefer"],
        "keywords": ["Fresh Produce", "Refrigerated Food"],
    },
    "Meat": {
        "equipment": ["Reefer"],
        "keywords": ["Meat", "Refrigerated Food"],
    },
    "Dairy": {
        "equipment": ["Reefer"],
        "keywords": ["Refrigerated Food"],
    },
    "Frozen Food": {
        "equipment": ["Reefer"],
        "keywords": ["Refrigerated Food"],
    },
    "Flowers": {
        "equipment": ["Reefer"],
        "keywords": ["Fresh Produce", "Refrigerated Food"],
    },
    "Beverages": {
        "equipment": ["Reefer"],
        "keywords": ["Beverages", "Refrigerated Food"],
    },
    "Steel": {
        "equipment": ["Flatbed"],
        "keywords": ["Metal: sheets, coils, rolls"],
    },
    "Lumber": {
        "equipment": ["Flatbed"],
        "keywords": ["Logs, Poles, Beams, Lumber"],
    },
    "Coils": {
        "equipment": ["Flatbed"],
        "keywords": ["Metal: sheets, coils, rolls"],
    },
    "Pipes": {
        "equipment": ["Flatbed"],
        "keywords": ["Building Materials", "Metal: sheets, coils, rolls"],
    },
    "Tall Machinery": {
        "equipment": ["Step Deck"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Tractors": {
        "equipment": ["Step Deck"],
        "keywords": ["Agricultural/Farm Supplies", "Machinery, Large Objects"],
    },
    "Containers": {
        "equipment": ["Step Deck"],
        "keywords": ["Intermodal Cont."],
    },
    "Large Equipment": {
        "equipment": ["Step Deck"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Excavators": {
        "equipment": ["RGN", "Lowboy"],
        "keywords": ["Machinery, Large Objects", "Construction"],
    },
    "Bulldozers": {
        "equipment": ["RGN", "Lowboy"],
        "keywords": ["Construction", "Machinery, Large Objects"],
    },
    "Cranes": {
        "equipment": ["RGN"],
        "keywords": ["Machinery, Large Objects", "Construction"],
    },
    "Transformers": {
        "equipment": ["RGN"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Wind Turbine Parts": {
        "equipment": ["RGN"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Heavy Construction Equipment": {
        "equipment": ["Lowboy"],
        "keywords": ["Construction", "Machinery, Large Objects"],
    },
    "Dozers": {
        "equipment": ["Lowboy"],
        "keywords": ["Construction", "Machinery, Large Objects"],
    },
    "Oversized Machinery": {
        "equipment": ["Lowboy"],
        "keywords": ["Machinery, Large Objects"],
    },
    "Paper": {
        "equipment": ["Dry Van"],
        "keywords": ["Paper Products"],
    },
    "Non-Perishable Food": {
        "equipment": ["Dry Van"],
        "keywords": ["General Freight"],
    },
    "Consumer Goods": {
        "equipment": ["Dry Van"],
        "keywords": ["General Freight"],
    },
}

# Equipment -> commodities (reverse view, for reference pages)
EQUIPMENT_COMMODITIES = {}
for _commodity, _info in COMMODITIES.items():
    for _eq in _info["equipment"]:
        EQUIPMENT_COMMODITIES.setdefault(_eq, []).append(_commodity)


def commodity_names():
    return sorted(COMMODITIES.keys())


def equipment_label(name):
    """Truck types for a commodity, e.g. 'Box Truck, Dry Van'.

    Uses the commodity's equipment list from COMMODITIES, in mapping
    order; '' when the commodity is unknown or has no equipment.
    """
    eq = COMMODITIES.get(name, {}).get("equipment") or []
    return ", ".join(eq)


def carrier_matches_commodity(cargo_carried, commodity):
    """True when a carrier's SAFER cargo list hauls the given commodity."""
    info = COMMODITIES.get(commodity)
    if not info:
        return False
    cargo = [c.strip().lower() for c in (cargo_carried or [])]
    for kw in info["keywords"]:
        if kw.lower() in cargo:
            return True
    return False


def filter_carriers_by_commodity(carriers, commodity):
    """carriers: list of dicts with a 'cargo_carried' key (list or string).

    commodity: one commodity name, or a list of names. Returns the
    carriers whose cargo hauls ANY of the named commodities.
    """
    if isinstance(commodity, str):
        wanted = [commodity]
    else:
        wanted = list(commodity or [])
    wanted = [w for w in wanted if w]
    if not wanted:
        return list(carriers)
    out = []
    for c in carriers:
        cargo = c.get("cargo_carried") or []
        if isinstance(cargo, str):
            cargo = [x.strip() for x in cargo.split(",") if x.strip()]
        if any(carrier_matches_commodity(cargo, w) for w in wanted):
            out.append(c)
    return out


# The artifact copy's Expected Truck rules (CARGO_TRUCK_RULES) check cargo
# most-specific first with General Freight last, and map it to exactly
# ['Dry Van', 'Box Truck'] — so a General Freight carrier shows those two
# only, in that order. Flask's vote-based estimate used to list six types
# for the same cargo. Direct commodity-name matches below use the
# artifact's order for General Freight; anything else keeps its
# COMMODITIES equipment order.
ARTIFACT_TRUCK_ORDER = {
    "General Freight": ["Dry Van", "Box Truck"],
}


def expected_trucks(cargo_carried):
    """Estimate a carrier's truck types from its SAFER cargo list.

    SAFER does not record truck type, so this is a best guess only.
    Tier 1 — direct match: a cargo item that IS a commodity name uses
    that commodity's equipment (General Freight in the artifact's rule
    order); matches accumulate in cargo order, deduped, max 6. Tier 2 —
    fallback: the vote-based estimate below, for cargo that names no
    commodity directly (unchanged behaviour). Returns [] when there is
    no cargo or nothing matches.
    """
    cargo = [c.strip() for c in (cargo_carried or []) if c and c.strip()]
    if not cargo:
        return []
    lowered = {name.lower(): (name, info)
               for name, info in COMMODITIES.items()}
    direct = []
    for item in cargo:
        hit = lowered.get(item.lower())
        if hit is None:
            continue
        name, info = hit
        eq = ARTIFACT_TRUCK_ORDER.get(name, info["equipment"])
        for e in eq:
            if e not in direct:
                direct.append(e)
        if len(direct) >= 6:
            break
    if direct:
        return direct[:6]
    votes = {}
    lowered_cargo = [c.lower() for c in cargo]
    for info in COMMODITIES.values():
        if any(kw.lower() in lowered_cargo for kw in info["keywords"]):
            for eq in info["equipment"]:
                votes[eq] = votes.get(eq, 0) + 1
    return sorted(votes, key=lambda eq: (-votes[eq], eq))
