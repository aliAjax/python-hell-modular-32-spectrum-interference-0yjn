from datetime import datetime, timezone

# 时频关联参数：区域相同、频点相差不超过 0.05MHz、观测时间相差不超过十分钟
FREQUENCY_TOLERANCE_MHZ = 0.05
TIME_WINDOW_MINUTES = 10
# 至少三个不同监测站形成共识，协调员才能停用干扰源
MIN_CONSENSUS_STATIONS = 3


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _observation_from_item(payload):
    return {
        "origin": "item",
        "station_id": payload.get("station_id"),
        "region": payload.get("region"),
        "frequency_mhz": payload.get("frequency_mhz"),
        "observed_at": payload.get("detected_at"),
        "strength_dbm": payload.get("strength_dbm"),
    }


def _observation_from_source(source):
    payload = source.get("payload") or {}
    return {
        "origin": "source",
        "source_id": source.get("id"),
        "external_id": source.get("external_id"),
        "source_type": source.get("source_type"),
        "station_id": payload.get("station_id"),
        "region": payload.get("region"),
        "frequency_mhz": payload.get("frequency_mhz"),
        "observed_at": source.get("observed_at"),
        "strength_dbm": payload.get("strength_dbm"),
    }


def _linkable(obs):
    return bool(
        obs["station_id"]
        and obs["region"]
        and isinstance(obs["frequency_mhz"], (int, float))
        and not isinstance(obs["frequency_mhz"], bool)
        and parse_time(obs["observed_at"]) is not None
    )


def _matches(a, b):
    if a["region"] != b["region"]:
        return False
    # 浮点频差比较留出微小余量，保证 0.05MHz 边界值被正确纳入
    if abs(float(a["frequency_mhz"]) - float(b["frequency_mhz"])) > FREQUENCY_TOLERANCE_MHZ + 1e-9:
        return False
    delta = parse_time(a["observed_at"]) - parse_time(b["observed_at"])
    return abs(delta.total_seconds()) <= TIME_WINDOW_MINUTES * 60


def _member_ref(obs):
    ref = {
        "origin": obs["origin"],
        "station_id": obs["station_id"],
        "observed_at": obs["observed_at"],
        "strength_dbm": obs["strength_dbm"],
    }
    if obs["origin"] == "source":
        ref["source_id"] = obs["source_id"]
        ref["external_id"] = obs["external_id"]
        ref["source_type"] = obs["source_type"]
    return ref


def _summarize(observations):
    observations = sorted(observations, key=lambda o: (parse_time(o["observed_at"]), str(o["station_id"])))
    stations = sorted({o["station_id"] for o in observations})
    strengths = [
        float(o["strength_dbm"])
        for o in observations
        if isinstance(o["strength_dbm"], (int, float)) and not isinstance(o["strength_dbm"], bool)
    ]
    frequencies = [float(o["frequency_mhz"]) for o in observations]
    return {
        "region": observations[0]["region"],
        "stations": stations,
        "station_count": len(stations),
        "earliest_observed_at": observations[0]["observed_at"],
        "latest_observed_at": observations[-1]["observed_at"],
        "strongest_signal_dbm": max(strengths) if strengths else None,
        "frequency_min_mhz": min(frequencies),
        "frequency_max_mhz": max(frequencies),
        "includes_primary": any(o["origin"] == "item" for o in observations),
        "members": [_member_ref(o) for o in observations],
    }


def correlate(item_payload, sources):
    """按时频关系把主观测与来源归成联合证据组。"""
    observations = [_observation_from_item(item_payload)]
    for source in sources:
        obs = _observation_from_source(source)
        # 来源未单独标注区域时，默认归属主记录区域
        if not obs["region"]:
            obs["region"] = item_payload.get("region")
        observations.append(obs)

    linkable = [o for o in observations if _linkable(o)]
    uncorrelated = [
        {"source_id": o["source_id"], "external_id": o["external_id"],
         "reason": "缺少站号、频点或观测时间，无法参与时频关联"}
        for o in observations
        if o["origin"] == "source" and not _linkable(o)
    ]

    parent = list(range(len(linkable)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(linkable)):
        for j in range(i + 1, len(linkable)):
            if _matches(linkable[i], linkable[j]):
                union(i, j)

    buckets = {}
    for i, obs in enumerate(linkable):
        buckets.setdefault(find(i), []).append(obs)

    groups = [_summarize(members) for members in buckets.values()]
    # 主记录所在组优先，其次按最早观测时间排序，保证组号稳定可读
    groups.sort(key=lambda g: (not g["includes_primary"], g["earliest_observed_at"]))
    for index, group in enumerate(groups, 1):
        group["id"] = "G%d" % index
        group["consensus"] = group["station_count"] >= MIN_CONSENSUS_STATIONS

    primary = next((g for g in groups if g["includes_primary"]), None)
    consensus = {
        "reached": bool(primary and primary["consensus"]),
        "required_stations": MIN_CONSENSUS_STATIONS,
        "stations": primary["stations"] if primary else [],
        "station_count": primary["station_count"] if primary else 0,
    }
    return {
        "frequency_tolerance_mhz": FREQUENCY_TOLERANCE_MHZ,
        "time_window_minutes": TIME_WINDOW_MINUTES,
        "min_consensus_stations": MIN_CONSENSUS_STATIONS,
        "groups": groups,
        "uncorrelated_sources": uncorrelated,
        "consensus": consensus,
    }


def group_for_source(digest, source_id):
    return next(
        (g for g in digest["groups"]
         if any(m.get("source_id") == source_id for m in g["members"])),
        None,
    )


def group_summary(group):
    return {
        "id": group["id"],
        "region": group["region"],
        "stations": group["stations"],
        "station_count": group["station_count"],
        "earliest_observed_at": group["earliest_observed_at"],
        "strongest_signal_dbm": group["strongest_signal_dbm"],
        "consensus": group["consensus"],
    }


def primary_group(digest):
    return next((g for g in digest["groups"] if g["includes_primary"]), None)


def consensus_gap(digest, expected_stations=None):
    """说明共识还缺哪些站：有期望站名单时指出具体站号，否则指出缺口数量。"""
    consensus = digest["consensus"]
    present = set(consensus["stations"])
    expected = [s for s in (expected_stations or []) if isinstance(s, str) and s.strip()]
    missing = [s for s in dict.fromkeys(expected) if s not in present]
    return {
        "present": sorted(present),
        "missing_stations": missing,
        "shortage": max(0, consensus["required_stations"] - consensus["station_count"]),
        "required": consensus["required_stations"],
    }
