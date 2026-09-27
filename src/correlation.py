"""时频关联：把同一区域内频点相近、观测时间相近的来源归为联合证据。

关联规则（两两判定，并查集求连通分量，允许链式传递）：
- 区域相同；
- 频点相差不超过 FREQUENCY_TOLERANCE_MHZ（0.05 MHz）；
- 观测时间相差不超过 TIME_WINDOW_SECONDS（十分钟）。

一个联合证据至少涉及 CONSENSUS_STATION_COUNT（3）个不同监测站时，
才构成停用干扰源所需的"共识"。
"""

from datetime import datetime, timezone, timedelta

FREQUENCY_TOLERANCE_MHZ = 0.05
TIME_WINDOW_SECONDS = 600
CONSENSUS_STATION_COUNT = 3
_EPSILON = 1e-9


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clean_station(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_observations(item_payload, sources):
    """把事件初始观测与全部来源展开为统一的观测点列表。

    来源未填区域/频点时，回退到所属事件的区域/频点（同一条干扰记录）。
    """
    observations = [
        {
            "ref": "item",
            "station_id": _clean_station(item_payload.get("station_id")),
            "region": item_payload.get("region"),
            "frequency_mhz": float(item_payload["frequency_mhz"]),
            "observed_at": item_payload["detected_at"],
            "strength_dbm": float(item_payload["strength_dbm"]),
        }
    ]
    for row in sources:
        source_payload = row["payload"]
        frequency = source_payload.get("frequency_mhz")
        if frequency is None:
            frequency = float(item_payload["frequency_mhz"])
        else:
            frequency = float(frequency)
        observations.append(
            {
                "ref": "src-%s" % row["id"],
                "station_id": _clean_station(source_payload.get("station_id")),
                "region": source_payload.get("region") or item_payload.get("region"),
                "frequency_mhz": frequency,
                "observed_at": row["observed_at"],
                "strength_dbm": float(source_payload["strength_dbm"]),
            }
        )
    return observations


def _related(a, b):
    if a["region"] != b["region"]:
        return False
    if abs(a["frequency_mhz"] - b["frequency_mhz"]) > FREQUENCY_TOLERANCE_MHZ + _EPSILON:
        return False
    delta = abs((parse_time(a["observed_at"]) - parse_time(b["observed_at"])).total_seconds())
    return delta <= TIME_WINDOW_SECONDS + _EPSILON


def _group(observations):
    """并查集：任一观测对满足时频阈值即连通，连通分量为一个联合证据簇。"""
    ordered = sorted(observations, key=lambda obs: (parse_time(obs["observed_at"]), obs["ref"]))
    parent = list(range(len(ordered)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if _related(ordered[i], ordered[j]):
                union(i, j)

    groups = {}
    for index, obs in enumerate(ordered):
        groups.setdefault(find(index), []).append(obs)
    return list(groups.values())


def _summarize_cluster(members, expected_stations):
    members = sorted(members, key=lambda obs: (parse_time(obs["observed_at"]), obs["ref"]))
    first = members[0]

    strongest = members[0]
    for obs in members[1:]:
        # 强度相同（dBm 越大越强）时保留观测时间更早的一条
        if obs["strength_dbm"] > strongest["strength_dbm"]:
            strongest = obs

    station_ids = []
    for obs in members:
        station = obs["station_id"]
        if station is not None and station not in station_ids:
            station_ids.append(station)

    station_count = len(station_ids)
    missing_count = max(0, CONSENSUS_STATION_COUNT - station_count)
    missing_stations = [s for s in (expected_stations or []) if s not in station_ids]

    return {
        "cluster_id": first["ref"],
        "region": first["region"],
        "frequency_mhz": first["frequency_mhz"],
        "min_frequency_mhz": min(obs["frequency_mhz"] for obs in members),
        "max_frequency_mhz": max(obs["frequency_mhz"] for obs in members),
        "first_observed_at": first["observed_at"],
        "last_observed_at": members[-1]["observed_at"],
        "strongest": {
            "source_ref": strongest["ref"],
            "station_id": strongest["station_id"],
            "strength_dbm": strongest["strength_dbm"],
            "observed_at": strongest["observed_at"],
        },
        "member_source_ids": [obs["ref"] for obs in members],
        "member_count": len(members),
        "station_ids": station_ids,
        "station_count": station_count,
        "joint": len(members) > 1,
        "consensus_reached": station_count >= CONSENSUS_STATION_COUNT,
        "missing_station_count": missing_count,
        "missing_stations": missing_stations,
    }


def summarize(observations, expected_stations=None):
    clusters = [_summarize_cluster(group, expected_stations) for group in _group(observations)]
    clusters.sort(key=lambda cluster: (parse_time(cluster["first_observed_at"]), cluster["cluster_id"]))
    consensus_ids = [cluster["cluster_id"] for cluster in clusters if cluster["consensus_reached"]]
    return {
        "thresholds": {
            "frequency_tolerance_mhz": FREQUENCY_TOLERANCE_MHZ,
            "time_window_seconds": TIME_WINDOW_SECONDS,
            "consensus_station_count": CONSENSUS_STATION_COUNT,
        },
        "clusters": clusters,
        "consensus_reached": bool(consensus_ids),
        "consensus_cluster_ids": consensus_ids,
    }


def build_change_event(before, after, new_ref):
    """对比纳入新来源前后的簇，生成审计用的关联变化记录。"""
    target = next(cluster for cluster in after["clusters"] if new_ref in cluster["member_source_ids"])
    previous_members = set(target["member_source_ids"]) - {new_ref}
    affected = [
        cluster
        for cluster in before["clusters"]
        if previous_members.intersection(cluster["member_source_ids"])
    ]

    if not previous_members:
        change = "formed"
    elif len(affected) == 1:
        change = "joined"
    else:
        change = "merged"

    return {
        "change": change,
        "source_ref": new_ref,
        "cluster_id": target["cluster_id"],
        "merged_cluster_ids": [cluster["cluster_id"] for cluster in affected] if change == "merged" else [],
        "station_ids": target["station_ids"],
        "station_count": target["station_count"],
        "consensus_reached": target["consensus_reached"],
        "before_clusters": affected,
        "after_cluster": target,
    }


def consensus_evidence(cluster):
    """随停用授权一并留档的共识依据快照（后续归并不再改写）。"""
    return {
        "cluster_id": cluster["cluster_id"],
        "station_ids": list(cluster["station_ids"]),
        "station_count": cluster["station_count"],
        "first_observed_at": cluster["first_observed_at"],
        "strongest": dict(cluster["strongest"]),
    }


def insufficient_consensus_message(summary):
    """证据不够时指出已有哪几站、仍缺哪一站/几个站。"""
    if not summary["clusters"]:
        return "尚无任何来源观测，至少需要 %d 个不同监测站形成共识" % CONSENSUS_STATION_COUNT
    target = sorted(
        summary["clusters"],
        key=lambda cluster: (
            -cluster["station_count"],
            -cluster["member_count"],
            parse_time(cluster["first_observed_at"]),
        ),
    )[0]
    if target["station_ids"]:
        stations = "、".join(target["station_ids"])
        message = "联合证据仅由 %d 个监测站（%s）形成，至少需要 %d 个不同监测站共识" % (
            target["station_count"],
            stations,
            CONSENSUS_STATION_COUNT,
        )
    else:
        message = "联合证据缺少可识别的监测站，至少需要 %d 个不同监测站共识" % CONSENSUS_STATION_COUNT
    if target["missing_stations"]:
        message += "；仍缺监测站：%s" % "、".join(target["missing_stations"])
    elif target["missing_station_count"]:
        message += "；仍缺 %d 个监测站的上报" % target["missing_station_count"]
    return message
