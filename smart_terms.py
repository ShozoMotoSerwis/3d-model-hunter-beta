from __future__ import annotations

import re
from itertools import product
from typing import Dict, List, Tuple

# Curated for technical / repair / workshop searches.
# The purpose is not perfect translation; it is finding the vocabulary used by model authors.
PL_CONCEPTS: Dict[str, List[str]] = {
    "suszarki do ubrań": ["drying rack", "clothes airer", "clothes drying rack", "laundry drying rack"],
    "suszarka do ubrań": ["drying rack", "clothes airer", "clothes drying rack", "laundry drying rack"],
    "suszarka na pranie": ["drying rack", "clothes airer", "clothes drying rack", "laundry drying rack"],
    "mocowanie tablicy": ["license plate holder", "license plate bracket", "number plate holder", "registration plate mount"],
    "uchwyt telefonu": ["phone holder", "phone mount", "smartphone holder", "smartphone mount"],
    "uchwyt czujnika": ["sensor holder", "sensor mount", "sensor bracket"],
    "uchwyt szpuli": ["spool holder", "spool mount", "filament spool holder"],
    "uchwyt kabli": ["cable holder", "cable clip", "wire holder", "cable management clip"],
    "osłona łańcucha": ["chain guard", "chain cover", "chain protector"],
    "prowadnik łańcucha": ["chain guide", "chain slider", "chain guide block"],
    "zacisk hamulcowy": ["brake caliper", "brake calliper"],
    "koło pasowe": ["pulley", "belt pulley", "drive pulley"],
    "koło zębate": ["gear", "cog", "cogwheel"],
    "zębatka napędowa": ["sprocket", "drive sprocket", "front sprocket"],
    "prowadnica filamentu": ["filament guide", "filament guide tube", "filament guide bracket"],
    "kierunkowskaz": ["turn signal", "indicator", "blinker", "turn indicator"],
    "uchwyt": ["holder", "mount", "bracket", "support"],
    "wspornik": ["bracket", "support", "mount", "brace"],
    "mocowanie": ["mount", "bracket", "holder", "fixture"],
    "adapter": ["adapter", "adaptor", "conversion adapter"],
    "przejściówka": ["adapter", "adaptor", "converter"],
    "zaślepka": ["cap", "plug", "end cap", "blanking plug"],
    "osłona": ["cover", "guard", "shield", "protector"],
    "obudowa": ["housing", "enclosure", "case", "shell"],
    "zębatka": ["gear", "cog", "pinion", "sprocket"],
    "tuleja": ["bushing", "sleeve", "spacer", "bush"],
    "dystans": ["spacer", "standoff", "distance sleeve"],
    "klips": ["clip", "retainer", "fastener"],
    "spinka": ["clip", "retainer", "fastener", "push clip"],
    "zatrzask": ["latch", "clip", "snap", "snap fit"],
    "pokrętło": ["knob", "dial", "adjustment knob"],
    "gałka": ["knob", "handle", "grip"],
    "rączka": ["handle", "grip", "hand grip"],
    "dźwignia": ["lever", "arm", "actuating lever"],
    "nakładka": ["cover", "cap", "sleeve", "overlay"],
    "wkładka": ["insert", "liner", "inlay"],
    "prowadnica": ["guide", "rail", "guide rail", "track"],
    "ślizg": ["slider", "guide", "slide", "wear pad"],
    "błotnik": ["fender", "mudguard", "mud guard"],
    "owiewka": ["fairing", "cowl", "body panel"],
    "podnóżek": ["footpeg", "foot peg", "footrest"],
    "stopka": ["stand", "foot", "kickstand", "side stand"],
    "lusterko": ["mirror", "rear view mirror"],
    "tablica rejestracyjna": ["license plate", "number plate", "registration plate"],
    "silniczek": ["motor", "actuator", "servo motor"],
    "klapka": ["flap", "door", "cover", "lid"],
    "przepustnica": ["throttle body", "throttle valve", "butterfly valve"],
    "kolektor": ["manifold", "collector", "header"],
    "dolot": ["intake", "air intake", "inlet"],
    "wydech": ["exhaust", "exhaust pipe", "muffler"],
    "obejma": ["clamp", "collar", "band clamp"],
    "opaska": ["clamp", "strap", "band"],
    "przewód": ["cable", "wire", "hose", "line"],
    "kostka": ["connector", "plug", "electrical connector"],
    "wtyczka": ["connector", "plug", "male connector"],
    "gniazdo": ["socket", "receptacle", "female connector"],
    "uszczelka": ["gasket", "seal", "sealing ring"],
    "uszczelniacz": ["seal", "oil seal", "shaft seal"],
    "łożysko": ["bearing", "ball bearing", "bushing"],
    "felga": ["rim", "wheel rim", "wheel"],
    "koło": ["wheel", "rim"],
    "śruba": ["bolt", "screw", "fastener"],
    "nakrętka": ["nut", "threaded nut"],
    "podkładka": ["washer", "spacer washer"],
    "gwint": ["thread", "screw thread", "threaded"],
    "tłumik": ["muffler", "silencer", "exhaust muffler"],
    "filtr": ["filter", "filter element"],
    "pojemnik": ["container", "box", "bin", "holder"],
    "stojak": ["stand", "rack", "holder"],
    "wieszak": ["hanger", "hook", "mount", "wall mount"],
    "hak": ["hook", "hanger"],
    "zawias": ["hinge", "pivot hinge", "joint", "pivot joint"],
    "sprężyna": ["spring", "coil spring"],
    "napinacz": ["tensioner", "belt tensioner", "chain tensioner"],
    "króciec": ["fitting", "nipple", "hose barb", "barb fitting"],
    "kolanko": ["elbow", "elbow fitting", "90 degree fitting"],
    "szybkozłączka": ["quick connector", "quick coupling", "quick coupler"],
    "zacisk": ["clamp", "clip", "caliper"],
    "zbiornik": ["tank", "reservoir", "container"],
    "korek": ["cap", "plug", "stopper"],
    "ramka": ["frame", "bezel", "surround"],
    "maskownica": ["bezel", "trim", "cover", "finisher"],
    "panel": ["panel", "trim panel", "cover panel"],
    "przycisk": ["button", "push button", "switch button"],
    "włącznik": ["switch", "power switch"],
    "przełącznik": ["switch", "selector", "toggle"],
    "pedał": ["pedal", "foot pedal"],
    "manetka": ["grip", "throttle grip", "handlebar grip"],
    "rolka": ["roller", "pulley", "wheel"],
    "czujnik": ["sensor", "detector", "sender"],
    "dysza": ["nozzle", "jet", "print nozzle"],
    "szpula": ["spool", "reel", "filament spool"],
    "organizer": ["organizer", "storage organizer", "sorting tray"],
}

EN_CONCEPTS: Dict[str, List[str]] = {
    "clothes dryer": ["drying rack", "clothes airer", "clothes drying rack", "laundry drying rack"],
    "drying rack": ["drying rack", "clothes airer", "clothes drying rack", "laundry drying rack"],
    "clothes airer": ["clothes airer", "drying rack", "clothes drying rack", "laundry drying rack"],
    "holder": ["holder", "mount", "bracket", "support"],
    "mount": ["mount", "bracket", "holder", "fixture"],
    "bracket": ["bracket", "mount", "support", "holder"],
    "cover": ["cover", "guard", "shield", "protector"],
    "housing": ["housing", "enclosure", "case", "shell"],
    "gear": ["gear", "cog", "pinion"],
    "sprocket": ["sprocket", "drive sprocket", "chain sprocket"],
    "bushing": ["bushing", "bush", "sleeve", "spacer"],
    "spacer": ["spacer", "standoff", "distance sleeve"],
    "clip": ["clip", "retainer", "fastener"],
    "turn signal": ["turn signal", "indicator", "blinker", "turn indicator"],
    "indicator": ["indicator", "turn signal", "blinker"],
    "fender": ["fender", "mudguard"],
    "fairing": ["fairing", "cowl", "body panel"],
    "cap": ["cap", "plug", "end cap"],
    "seal": ["seal", "gasket", "sealing ring"],
    "knob": ["knob", "dial", "control knob"],
    "handle": ["handle", "grip", "hand grip"],
    "guide": ["guide", "rail", "track"],
    "connector": ["connector", "plug", "socket"],
    "stand": ["stand", "rack", "holder"],
    "hanger": ["hanger", "hook", "wall mount"],
    "hinge": ["hinge", "pivot hinge", "joint", "pivot joint"],
}

STOPWORDS = {
    "do", "dla", "od", "z", "ze", "na", "w", "we", "i", "oraz", "a", "the", "for", "of", "to", "with", "and",
    "model", "models", "3d", "print", "printing", "druk", "druku", "drukarki", "drukarka", "free", "download"
}

TOKEN_RE = re.compile(r"[A-Za-zÀ-ž0-9][A-Za-zÀ-ž0-9_.+\-/]*", re.UNICODE)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _uniq(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        v = re.sub(r"\s+", " ", str(value).strip())
        k = v.lower()
        if v and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _english_aliases_for(synonyms: List[str]) -> List[str]:
    target = {_norm(x) for x in synonyms}
    aliases: List[str] = []
    for key, vals in EN_CONCEPTS.items():
        family = {_norm(x) for x in vals}
        if target & family:
            aliases.append(key)
    return aliases


def detect_concepts(original_pl: str, translated_en: str) -> List[Tuple[str, List[str]]]:
    hits: List[Tuple[str, List[str]]] = []
    op = _norm(original_pl)
    en = _norm(translated_en)

    occupied: List[str] = []
    for key in sorted(PL_CONCEPTS, key=len, reverse=True):
        if key in op:
            # Don't add a shorter concept fully contained in a longer one already matched.
            if any(key in longer for longer in occupied):
                continue
            hits.append((key, PL_CONCEPTS[key]))
            occupied.append(key)

    en_occupied: List[str] = []
    for key in sorted(EN_CONCEPTS, key=len, reverse=True):
        if key in en:
            if any(key in longer for longer in en_occupied):
                continue
            vals = EN_CONCEPTS[key]
            # Skip duplicate semantic families already detected from Polish.
            if not any(set(map(_norm, vals)) & set(map(_norm, existing)) for _, existing in hits):
                hits.append((key, vals))
                en_occupied.append(key)

    return hits[:4]


def _remove_concept_phrases(text: str, concepts: List[Tuple[str, List[str]]]) -> str:
    out = f" {text} "
    needles: List[str] = []
    for key, syns in concepts:
        needles.append(key)
        needles.extend(syns)
        needles.extend(_english_aliases_for(syns))
    for needle in sorted(_uniq(needles), key=len, reverse=True):
        out = re.sub(rf"(?<!\w){re.escape(needle)}(?!\w)", " ", out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


def _extract_anchor_data(original: str, translated: str, concepts: List[Tuple[str, List[str]]]) -> tuple[List[str], List[str], List[str]]:
    """Return (base_terms, hard_anchors, soft_terms).

    base_terms are semantic words left after removing detected concept families.
    hard anchors are brand/model/OEM-like tokens and are used for precision filtering.
    """
    residual = _remove_concept_phrases(translated, concepts)
    original_residual = _remove_concept_phrases(original, concepts)

    base_terms: List[str] = []
    for tok in TOKEN_RE.findall(residual):
        if tok.lower() not in STOPWORDS and len(tok) >= 2:
            base_terms.append(tok)
    base_terms = _uniq(base_terms)[:10]

    hard: List[str] = []
    original_tokens = TOKEN_RE.findall(original_residual)
    for idx, tok in enumerate(original_tokens):
        low = tok.lower()
        if low in STOPWORDS or len(tok) < 2:
            continue
        has_digit = any(ch.isdigit() for ch in tok)
        allcaps = tok.isupper() and len(tok) >= 2
        # Capitalized token away from sentence start is often a brand/product family (Vileda, Yamaha, Audi).
        capitalized_brandlike = tok[:1].isupper() and len(tok) >= 4
        modelish = bool(re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", tok)) or "-" in tok
        if has_digit or allcaps or capitalized_brandlike or modelish:
            hard.append(tok)

    # Hard anchors come ONLY from the user's original text.
    # A machine translator is allowed to suggest vocabulary, but never to invent a brand/OEM anchor.
    hard = _uniq(hard)[:8]

    soft = [t for t in base_terms if t.lower() not in {x.lower() for x in hard}]
    return base_terms, hard, soft


def build_variants(original: str, translated: str, max_variants: int = 24) -> dict:
    original = re.sub(r"\s+", " ", original.strip())
    translated = re.sub(r"\s+", " ", translated.strip()) or original
    concepts = detect_concepts(original, translated)
    base_terms, hard_anchors, soft_terms = _extract_anchor_data(original, translated, concepts)

    base = " ".join(base_terms).strip()
    variants: List[str] = []

    def add(v: str):
        v = re.sub(r"\s+", " ", v.strip())
        if v and v.lower() not in {x.lower() for x in variants}:
            variants.append(v)

    # The most important change versus 0.3: short natural-language semantic queries come FIRST.
    # We deliberately INTERLEAVE synonym families. That way even an adapter that only executes
    # the first 3-4 queries sees both a device-name alternative and a part-name alternative.
    if concepts:
        families = [vals[:4] for _, vals in concepts[:3]]
        primary = [f[0] for f in families]
        add(" ".join([base, *primary]).strip())

        max_alt = max((len(f) for f in families), default=1)
        for alt_idx in range(1, max_alt):
            for family_idx, family in enumerate(families):
                if alt_idx >= len(family):
                    continue
                combo = primary.copy()
                combo[family_idx] = family[alt_idx]
                add(" ".join([base, *combo]).strip())

        # Broader probes recover models whose title omits one of the concepts; the local ranker
        # still enforces brand/model anchors and checks the returned title/description.
        if base:
            for family in families:
                add(" ".join([base, family[0]]).strip())

        # Finally add a few true cross-combinations for long-tail wording.
        for combo in product(*families):
            add(" ".join([base, *combo]).strip())
            if len(variants) >= max_variants - 2:
                break
    else:
        add(translated)

    # Literal translation is a fallback, not a mandatory phrase.
    add(translated)
    if original.lower() != translated.lower():
        add(original)

    return {
        "original": original,
        "translated": translated,
        "concepts": [{"term": k, "synonyms": v} for k, v in concepts],
        "anchors": hard_anchors,
        "base_terms": base_terms,
        "soft_terms": soft_terms,
        "variants": variants[:max_variants],
    }


def build_search_plan(original: str, translated: str, max_queries: int = 12) -> dict:
    data = build_variants(original, translated, max_variants=max(24, max_queries * 2))
    english_queries = []
    for q in data["variants"]:
        if re.search(r"[ąćęłńóśźż]", q.lower()):
            continue
        english_queries.append(q)
    data["search_queries"] = _uniq(english_queries)[:max_queries]
    if not data["search_queries"]:
        data["search_queries"] = [translated or original]
    return data


def build_boolean_query(original: str, translated: str, formats: List[str] | None = None) -> tuple[str, dict]:
    """Backwards-compatible helper for the UI/debug panel.

    0.4 no longer uses this Boolean string as the primary search input. It fan-outs plain semantic queries instead.
    """
    data = build_search_plan(original, translated)
    concepts = data["concepts"]
    parts = list(data.get("anchors") or []) + list(data.get("soft_terms") or [])
    for c in concepts[:3]:
        syns = c["synonyms"][:4]
        safe = [f'"{s}"' if " " in s else s for s in syns]
        parts.append("(" + " OR ".join(safe) + ")")
    query = " ".join(parts).strip() or translated or original
    # Formats deliberately are NOT forced into the web query in 0.4; doing so killed recall.
    return query, data
