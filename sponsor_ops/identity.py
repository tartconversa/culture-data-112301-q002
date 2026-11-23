"""家庭身份：明确联系并按生效时间去重，未裁定联系保留为范围。

- confirmed 联系按生效日并入并查集；同一家庭跨设备、跨平台只算一个。
- possible 联系不并入明确家庭，作为"可能联系"单独披露。
- 无任何联系证据的设备保留为未关联，不能强行分配家庭。
- 迟到证据按其 effective_from 生效，但 received_on 决定当时是否已知。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# 实体的稳定表示：("household", 家庭) / ("possible", 可能家庭) / ("device", 设备)
Entity = tuple[str, str]


@dataclass
class HouseholdGraph:
    links: list[dict]

    def __post_init__(self) -> None:
        self._confirmed = [h for h in self.links if h["status"] == "confirmed"]
        self._possible = [h for h in self.links if h["status"] == "possible"]

    def _confirmed_as_of(self, on: str) -> list[dict]:
        return [h for h in self._confirmed if h["effective_from"] <= on]

    def resolve(self, device: str, on: str) -> Entity:
        """在业务日 on 的已生效明确联系下解析设备所属家庭。"""
        candidate = None
        for link in self._confirmed_as_of(on):
            if device in link["devices"]:
                candidate = link
                break
        if candidate is not None:
            return ("household", self._root(candidate["household"], on))
        for link in self._possible:
            if link["effective_from"] <= on and device in link["devices"]:
                return ("possible", link["household"])
        return ("device", device)

    def _root(self, household: str, on: str) -> str:
        """迟到合并证据把多个已确认家庭连在一起时，取规范名。"""
        active = self._confirmed_as_of(on)
        parent = {link["household"]: link["household"] for link in active}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        owners: dict[str, str] = {}
        for link in active:
            for device in link["devices"]:
                if device in owners:
                    union(owners[device], link["household"])
                else:
                    owners[device] = link["household"]
        return find(household)

    def known_on(
        self,
        device: str,
        on: str,
        received_by: str | None = None,
        only_link: str | None = None,
    ) -> bool:
        """在 on 时是否已有明确联系。

        生效时间决定联系在业务上何时成立；received_by 决定商务在哪个日期
        已经有这条证据。only_link 用于检查某一条具体证据是否已经到达。
        """
        for link in self._confirmed_as_of(on):
            if device not in link["devices"]:
                continue
            if only_link is not None and link["id"] != only_link:
                continue
            if received_by is None or link["received_on"] <= received_by:
                return True
        return False

    def possible_household(self, device: str, on: str) -> str | None:
        for link in self._possible:
            if link["effective_from"] <= on and device in link["devices"]:
                return link["household"]
        return None

    def canonical(self, household: str, on: str) -> str:
        """把调查中的家庭标识映射到经迟到证据合并后的规范家庭。"""
        return self._root(household, on)

    def device_links(self, device: str) -> list[dict]:
        return [h for h in self.links if device in h["devices"]]


def merge_late_links(base: list[dict], later_evidence: Iterable[dict]) -> list[dict]:
    links = [dict(h) for h in base]
    for evidence in later_evidence:
        if evidence.get("kind") != "household_confirm":
            continue
        payload = evidence["payload"]
        links.append(
            {
                "id": evidence["id"],
                "devices": list(payload["devices"]),
                "household": payload["household"],
                "status": "confirmed",
                "effective_from": payload.get("effective_from", evidence["received_on"]),
                "received_on": evidence["received_on"],
            }
        )
    return links


def device_entities(devices: Iterable[str], graph: HouseholdGraph, on: str) -> dict[str, Entity]:
    return {device: graph.resolve(device, on) for device in devices}
