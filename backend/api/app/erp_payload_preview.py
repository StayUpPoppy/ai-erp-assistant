from __future__ import annotations

from typing import Any, Dict, List

from app.schemas import IngestionResponse


def _pick(payload: Dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _safe_float(raw: str | None, fallback: float) -> float:
    if raw is None or not str(raw).strip():
        return fallback
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return fallback


def build_datynk_sale_order_payload(
    ingestion: IngestionResponse,
    *,
    default_org: str = "",
    default_sales_user: str = "",
) -> Dict[str, Any]:
    fields = dict(ingestion.resolved_fields or {})
    preview = ingestion.preview_data
    if preview is not None:
        order = preview.order
        fields.update(
            {
                "org": order.org,
                "customerName": order.customerName,
                "customerPoNo": order.customerPoNo,
                "salesUser": order.salesUser,
                "doc_date": order.orderDate,
                "orderStatus": order.orderStatus,
                "deliveryAddr": order.deliveryAddr,
                "rate": "" if order.rate is None else str(order.rate),
                "currency": order.currency,
                "delivery_date": order.deliveryDate,
                "deliveryDate": order.deliveryDate,
            }
        )

    org = _pick(fields, "org", "org_name") or default_org or ingestion.org_id
    customer = _pick(fields, "customerName", "customer_name", "vendor_code")
    material = _pick(fields, "material_code", "materialCode")
    qty = _safe_float(_pick(fields, "line_qty", "qty") or "1", 1.0)
    if qty <= 0:
        qty = 1.0
    doc_date = _pick(fields, "doc_date", "orderDate", "order_date")
    currency = _pick(fields, "currency") or "CNY"
    delivery_date = _pick(fields, "deliveryDate", "delivery_date", "jhq") or doc_date
    rate = _safe_float(_pick(fields, "rate"), 1.0)
    sales_user = _pick(fields, "salesUser", "sales_user") or default_sales_user
    order_status = _pick(fields, "orderStatus", "order_status") or "pending"
    delivery_addr = _pick(fields, "deliveryAddr", "delivery_addr", "delivery_address")
    customer_po = _pick(fields, "customerPoNo", "customer_po_no")

    details: List[Dict[str, Any]] = []
    if preview is not None and preview.details:
        for detail in preview.details:
            details.append(
                {
                    "materialCode": detail.materialCode,
                    "productName": detail.productName or detail.materialCode,
                    "productSpec": detail.productSpec,
                    "ph": detail.ph,
                    "customerMaterialNo": detail.customerMaterialNo,
                    "qty": detail.qty if detail.qty is not None else qty,
                    "price": detail.price if detail.price is not None else 1.0,
                    "taxPrice": detail.taxPrice if detail.taxPrice is not None else 1.13,
                    "amount": detail.amount if detail.amount is not None else round(qty * 1.0, 10),
                    "allAmount": detail.allAmount if detail.allAmount is not None else round(qty * 1.13, 10),
                    "tax": detail.tax if detail.tax is not None else 13,
                    "taxAmount": detail.taxAmount if detail.taxAmount is not None else round(qty * 0.13, 10),
                    "gift": detail.gift,
                    "remark": detail.remark,
                }
            )
    if not details:
        price = _safe_float(_pick(fields, "unit_price", "line_price", "price"), 1.0)
        if price <= 0:
            price = 1.0
        tax_pct = _safe_float(_pick(fields, "tax", "tax_rate"), 13.0)
        amount = round(price * qty, 10)
        tax_amount = round(amount * (tax_pct / 100.0), 10)
        all_amount = round(amount + tax_amount, 10)
        details.append(
            {
                "materialCode": material,
                "productName": _pick(fields, "productName", "product_name") or material,
                "productSpec": _pick(fields, "productSpec", "product_spec"),
                "ph": _pick(fields, "ph", "material_ph"),
                "customerMaterialNo": _pick(fields, "customerMaterialNo", "customer_material_no"),
                "qty": qty,
                "price": price,
                "taxPrice": round(all_amount / qty, 10) if qty else all_amount,
                "amount": amount,
                "allAmount": all_amount,
                "tax": int(tax_pct) if abs(tax_pct - int(tax_pct)) < 1e-9 else tax_pct,
                "taxAmount": tax_amount,
                "gift": _pick(fields, "gift", "line_gift").lower() in {"1", "true", "yes", "on"},
                "remark": _pick(fields, "line_remark", "detail_remark"),
            }
        )

    return {
        "order": {
            "org": org,
            "customerName": customer,
            "customerPoNo": customer_po,
            "salesUser": sales_user,
            "orderDate": doc_date,
            "orderStatus": order_status,
            "deliveryAddr": delivery_addr,
            "rate": rate,
            "currency": currency,
            "deliveryDate": delivery_date,
        },
        "details": details,
    }
