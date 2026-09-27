from . import correlation, domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

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

    def _correlator(self, source_type, external_id):
        def correlate_in_transaction(item_payload, sources_before, sources_after, new_source):
            before = correlation.correlate(item_payload, sources_before)
            after = correlation.correlate(item_payload, sources_after)
            assigned = correlation.group_for_source(after, new_source["id"])
            new_payload = dict(item_payload)
            new_payload["correlation"] = after
            primary = correlation.primary_group(after)
            event = {
                "source": {
                    "id": new_source["id"],
                    "source_type": source_type,
                    "external_id": external_id,
                },
                "assigned_group": assigned["id"] if assigned else None,
                "stations_before": before["consensus"]["stations"],
                "stations_after": after["consensus"]["stations"],
                "groups_before": len(before["groups"]),
                "groups_after": len(after["groups"]),
                "consensus_before": before["consensus"]["reached"],
                "consensus_after": after["consensus"]["reached"],
                "primary_summary": correlation.group_summary(primary) if primary else None,
            }
            return new_payload, event

        return correlate_in_transaction

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        result = self.repository.add_source(
            item_id,
            source_type,
            external_id,
            normalized,
            observed_at,
            actor,
            role,
            correlator=self._correlator(source_type, external_id),
        )
        result.pop("item_payload", None)
        return result

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
        sources = None
        if action in rules.ACTION_NEEDS_SOURCES:
            sources = self.repository.list_sources(item_id)
        new_status, new_payload, event_payload = rules.apply_action(
            item, action, payload, actor, role, sources=sources
        )
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        stored = item["payload"].get("correlation")
        item["correlation"] = stored or correlation.correlate(item["payload"], item["sources"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
