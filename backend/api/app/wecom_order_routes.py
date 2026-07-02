from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal, is_database_enabled
from app.orm_models import WecomOrderRouteRow


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def _route_id() -> str:
    return str(uuid4())


@dataclass
class WecomOrderRoute:
    route_id: str
    wecom_group_id: str
    wecom_group_name: str
    customer_name: str
    factory_name: str
    erp_user_id: str
    sales_user_name: str
    org_id: str
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class _InMemoryWecomRouteStore:
    routes: Dict[str, WecomOrderRoute]
    lock: Lock


wecom_route_store = _InMemoryWecomRouteStore(routes={}, lock=Lock())


def _row_to_route(row: WecomOrderRouteRow) -> WecomOrderRoute:
    return WecomOrderRoute(
        route_id=row.route_id,
        wecom_group_id=row.wecom_group_id or "",
        wecom_group_name=row.wecom_group_name or "",
        customer_name=row.customer_name or "",
        factory_name=row.factory_name or "",
        erp_user_id=row.erp_user_id,
        sales_user_name=row.sales_user_name or "",
        org_id=row.org_id,
        enabled=bool(row.enabled),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
    )


def _apply_route_to_row(row: WecomOrderRouteRow, route: WecomOrderRoute) -> None:
    row.wecom_group_id = _norm(route.wecom_group_id) or None
    row.wecom_group_name = _norm(route.wecom_group_name) or None
    row.customer_name = _norm(route.customer_name) or None
    row.factory_name = _norm(route.factory_name) or None
    row.erp_user_id = _norm(route.erp_user_id)
    row.sales_user_name = _norm(route.sales_user_name) or None
    row.org_id = _norm(route.org_id)
    row.enabled = bool(route.enabled)
    row.created_at = route.created_at or _now_iso()
    row.updated_at = route.updated_at or _now_iso()


def _new_row(route: WecomOrderRoute) -> WecomOrderRouteRow:
    row = WecomOrderRouteRow(route_id=route.route_id or _route_id())
    _apply_route_to_row(row, route)
    return row


def upsert_wecom_order_route(route: WecomOrderRoute) -> WecomOrderRoute:
    now = _now_iso()
    saved = WecomOrderRoute(
        route_id=route.route_id or _route_id(),
        wecom_group_id=_norm(route.wecom_group_id),
        wecom_group_name=_norm(route.wecom_group_name),
        customer_name=_norm(route.customer_name),
        factory_name=_norm(route.factory_name),
        erp_user_id=_norm(route.erp_user_id),
        sales_user_name=_norm(route.sales_user_name),
        org_id=_norm(route.org_id),
        enabled=bool(route.enabled),
        created_at=route.created_at or now,
        updated_at=now,
    )
    if not saved.erp_user_id or not saved.org_id:
        raise ValueError("erp_user_id and org_id are required")

    if is_database_enabled():
        assert SessionLocal is not None
        session = SessionLocal()
        try:
            row = session.get(WecomOrderRouteRow, saved.route_id)
            if row is None:
                row = _new_row(saved)
                session.add(row)
            else:
                _apply_route_to_row(row, saved)
            session.commit()
            return _row_to_route(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    with wecom_route_store.lock:
        wecom_route_store.routes[saved.route_id] = saved
    return saved


def clear_wecom_order_routes_for_tests() -> None:
    with wecom_route_store.lock:
        wecom_route_store.routes.clear()


def _enabled(routes: List[WecomOrderRoute]) -> List[WecomOrderRoute]:
    return [route for route in routes if route.enabled]


def _resolve_from_memory(
    *,
    customer_name: str,
    wecom_group_id: str,
    wecom_group_name: str,
    customer_name_hint: str,
    factory_name_hint: str,
) -> Optional[WecomOrderRoute]:
    with wecom_route_store.lock:
        routes = _enabled(list(wecom_route_store.routes.values()))
    if customer_name:
        for route in routes:
            if route.customer_name == customer_name:
                return route
    if wecom_group_id:
        for route in routes:
            if route.wecom_group_id == wecom_group_id:
                return route
    if wecom_group_name:
        for route in routes:
            if route.wecom_group_name == wecom_group_name:
                return route
    if customer_name_hint and factory_name_hint:
        for route in routes:
            if route.customer_name == customer_name_hint and route.factory_name == factory_name_hint:
                return route
    return None


def _first_db_match(session, field: str, value: str) -> Optional[WecomOrderRoute]:
    if not value:
        return None
    col = getattr(WecomOrderRouteRow, field)
    row = session.execute(
        select(WecomOrderRouteRow)
        .where(col == value, WecomOrderRouteRow.enabled.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_route(row) if row else None


def resolve_wecom_order_route(
    *,
    customer_name: Optional[str] = None,
    wecom_group_id: Optional[str] = None,
    wecom_group_name: Optional[str] = None,
    customer_name_hint: Optional[str] = None,
    factory_name_hint: Optional[str] = None,
) -> Optional[WecomOrderRoute]:
    customer_exact = _norm(customer_name)
    gid = _norm(wecom_group_id)
    gname = _norm(wecom_group_name)
    customer = _norm(customer_name_hint)
    factory = _norm(factory_name_hint)

    if is_database_enabled():
        assert SessionLocal is not None
        session = SessionLocal()
        try:
            found = _first_db_match(session, "customer_name", customer_exact)
            if found:
                return found
            found = _first_db_match(session, "wecom_group_id", gid)
            if found:
                return found
            found = _first_db_match(session, "wecom_group_name", gname)
            if found:
                return found
            if customer and factory:
                row = session.execute(
                    select(WecomOrderRouteRow)
                    .where(
                        WecomOrderRouteRow.customer_name == customer,
                        WecomOrderRouteRow.factory_name == factory,
                        WecomOrderRouteRow.enabled.is_(True),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                return _row_to_route(row) if row else None
            return None
        finally:
            session.close()

    return _resolve_from_memory(
        customer_name=customer_exact,
        wecom_group_id=gid,
        wecom_group_name=gname,
        customer_name_hint=customer,
        factory_name_hint=factory,
    )
