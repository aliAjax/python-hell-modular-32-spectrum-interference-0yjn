from . import correlation, domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def correlation_for(self, item):
        observations = correlation.build_observations(item["payload"], item.get("sources", []))
        return correlation.summarize(observations, item["payload"].get("expected_stations"))

    def _consensus_cluster(self, item):
        summary = self.correlation_for(item)
        reached = [cluster for cluster in summary["clusters"] if cluster["consensus_reached"]]
        if not reached:
            raise DomainError(
                "insufficient_consensus",
                correlation.insufficient_consensus_message(summary),
                409,
            )
        reached.sort(key=lambda cluster: (cluster["first_observed_at"], cluster["cluster_id"]))
        return reached[0]

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        expected_stations = item["payload"].get("expected_stations")

        def on_recorded(conn, source_id):
            item_payload, sources = self.repository._correlation_inputs(conn, item_id)
            observations = correlation.build_observations(item_payload, sources)
            after = correlation.summarize(observations, expected_stations)
            new_observations = [obs for obs in observations if obs["ref"] != "src-%s" % source_id]
            before = correlation.summarize(new_observations, expected_stations)
            change = correlation.build_change_event(before, after, "src-%s" % source_id)
            self.repository.append_audit(conn, item_id, "correlation_updated", actor, role, change)
            target = next(cluster for cluster in after["clusters"] if cluster["cluster_id"] == change["cluster_id"])
            return {"correlation_change": change, "cluster": target, "correlation": after}

        return self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
            on_recorded,
        )

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        evidence = None
        if action == "suspend":
            # 至少三个不同监测站形成共识后，协调员才能停用干扰源
            item["sources"] = self.repository.list_sources(item_id)
            evidence = correlation.consensus_evidence(self._consensus_cluster(item))
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, evidence)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["correlation"] = self.correlation_for(item)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
