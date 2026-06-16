from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.structured_extract import extract_po_cn_layout_entities, extract_structured_fields, required_field_keys


def test_required_field_keys_by_doc_type():
    assert required_field_keys("PO") == [
        "vendor_code",
        "doc_date",
        "currency",
        "material_code",
        "line_qty",
    ]
    assert required_field_keys("GR") == [
        "vendor_code",
        "doc_date",
        "currency",
        "po_no",
        "material_code",
        "qty_received",
    ]
    assert required_field_keys("INV") == [
        "vendor_code",
        "doc_date",
        "currency",
        "invoice_no",
        "invoice_date",
    ]
    assert required_field_keys(None) == required_field_keys("PO")


def test_extract_po_material_and_qty():
    text = "PO header\nMaterial M010\nQty 8\n"
    got = extract_structured_fields(text, "PO")
    assert got.get("material_code") == "M010"
    assert got.get("line_qty") == "8"


def test_extract_gr_po_and_received():
    text = "GR\nPO-2026001\nM020\n收货数量 15\n"
    got = extract_structured_fields(text, "GR")
    assert got.get("po_no")
    assert got.get("material_code") == "M020"
    assert got.get("qty_received") == "15"


def test_extract_inv_invoice():
    text = "Tax Invoice\nInvoice No INV-7788\nInvoice Date 2026-03-01\n"
    got = extract_structured_fields(text, "INV")
    assert got.get("invoice_no") == "INV-7788"
    assert got.get("invoice_date") == "2026-03-01"


def test_extract_po_order_qty():
    got = extract_structured_fields("Purchase order line\nOrder Qty 24\nM030\n", "PO")
    assert got.get("material_code") == "M030"
    assert got.get("line_qty") == "24"


def test_extract_po_english_order_no_keeps_po_prefix():
    got = extract_structured_fields("Purchase Order\nOrder No.: POGSVC2600205\n", "PO")
    assert got.get("customerPoNo") == "POGSVC2600205"


def test_extract_po_total_qty_chinese():
    got = extract_structured_fields("明细\n合计数量：120\n", "PO")
    assert got.get("line_qty") == "120"


def test_extract_material_code_cn_label():
    got = extract_structured_fields("物料号：ABC-12.3\n", "PO")
    assert got.get("material_code") == "ABC-12.3"


def test_extract_po_cn_layout_header_and_one_line():
    text = (
        "采购订单\n"
        "供方：浙江英科弹簧科技有限公司\n"
        "需方：无锡智能自控工程股份有限公司\n"
        "订单编号：SC01P002603020026\n"
        "1 P3B0A305000335 压缩弹簧 4 件 100 88.495 400.00 2026/5/10\n"
    )
    got = extract_po_cn_layout_entities(text)
    assert got["supplier_name"] == "浙江英科弹簧科技有限公司"
    assert got["buyer_name"] == "无锡智能自控工程股份有限公司"
    assert got["order_no"] == "SC01P002603020026"
    assert "line_items_json" in got
    import json

    rows = json.loads(got["line_items_json"])
    assert len(rows) == 1
    assert rows[0]["inventory_code"] == "P3B0A305000335"
    assert rows[0]["quantity"] == "4"
    assert rows[0]["unit_price_incl_tax"] == "100"
    assert rows[0]["unit_price_excl_tax"] == "88.495"
    assert rows[0]["delivery_date"] == "2026-05-10"


def test_extract_po_cn_layout_two_lines():
    text = (
        "供方：A公司\n需方：B公司\n订单编号：PO-001\n"
        "1 CODE111111111111 弹簧A 1 件 10 8 10 2026/1/1\n"
        "2 CODE222222222222 弹簧B 2 件 20 16 40 2026/1/2\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert len(rows) == 2
    assert rows[1]["inventory_code"] == "CODE222222222222"
    assert rows[1]["quantity"] == "2"


def test_extract_po_cn_layout_mixed_case_inventory_code():
    text = (
        "供方：测试供方\n订单编号：PO-MIX-01\n"
        "1 ab12CD34ef56 螺母 10 件 1.5 1.2 15 2026/3/1\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert len(rows) == 1
    assert rows[0]["inventory_code"] == "ab12CD34ef56"
    assert rows[0]["quantity"] == "10"


def test_extract_gr_po_number_and_received_quantity():
    text = (
        "Goods Receipt\n"
        "PO Number: 4500123987\n"
        "Material M040\n"
        "Received quantity 7\n"
    )
    got = extract_structured_fields(text, "GR")
    assert got.get("po_no") == "4500123987"
    assert got.get("material_code") == "M040"
    assert got.get("qty_received") == "7"


def test_extract_gr_reference_po():
    text = "GR\nRef PO 88AB-12\nM041\nqty received: 2\n"
    got = extract_structured_fields(text, "GR")
    assert got.get("po_no") == "88AB-12"
    assert got.get("qty_received") == "2"


def test_extract_inv_invoice_number_and_date_of_invoice():
    text = "Commercial invoice\nInvoice Number: CI-2026-0007\nDate of invoice 2026-04-15\n"
    got = extract_structured_fields(text, "INV")
    assert got.get("invoice_no") == "CI-2026-0007"
    assert got.get("invoice_date") == "2026-04-15"


def test_extract_inv_bill_no():
    got = extract_structured_fields("Commercial\nBill No. BN-9090\n2026-02-01\n", "INV")
    assert got.get("invoice_no") == "BN-9090"


def test_extract_inv_token_after_header():
    got = extract_structured_fields("Header INV-001122\n2026-01-20\n", "INV")
    assert got.get("invoice_no") == "INV-001122"


def test_extract_inv_slash_style_number():
    got = extract_structured_fields("Tax invoice\nINV/2026/0044\n2026-02-01\n", "INV")
    assert got.get("invoice_no") == "INV/2026/0044"


def test_extract_po_cn_order_qty():
    got = extract_structured_fields("明细\n订购数量：12\n物料 M099\n", "PO")
    assert got.get("line_qty") == "12"
    assert got.get("material_code") == "M099"


def test_extract_gr_cn_purchase_order_no():
    text = "收货\n采购订单：4500123988\n物料 M100\n收货数量 3\n"
    got = extract_structured_fields(text, "GR")
    assert got.get("po_no") == "4500123988"
    assert got.get("qty_received") == "3"


def test_extract_inv_cn_invoice_no_label():
    got = extract_structured_fields("销项\n发票号：FP-2026-8899\n开票日期 2026-05-01\n", "INV")
    assert got.get("invoice_no") == "FP-2026-8899"
    assert got.get("invoice_date") == "2026-05-01"


def test_extract_gr_order_number_cn_label():
    text = "入库\n订单编号：5500123001\n物料 M210\n实收：9\n"
    got = extract_structured_fields(text, "GR")
    assert got.get("po_no") == "5500123001"
    assert got.get("material_code") == "M210"
    assert got.get("qty_received") == "9"


def test_extract_po_from_csv_like_pipe_row():
    """模拟 document_extract CSV 展开后的「列 | 列」正文。"""
    text = "vendor | doc_date | material | qty\nV003 | 2026-04-01 | M077 | 22\n"
    got = extract_structured_fields(text, "PO")
    assert got.get("material_code") == "M077"
    assert got.get("line_qty") == "22"


def test_extract_datynk_po_header_fields_without_llm():
    text = (
        "销售订单\n"
        "客户名称：北京优向国际能源装备有限公司\n"
        "客户采购单号：PO-2026-0008\n"
        "订单日期：2026年5月28日\n"
        "币别：CNY\n"
        "交货日期：2026/6/15\n"
        "物料号：S01P019430\n"
        "数量：12 件\n"
        "含税单价：2.50\n"
    )
    got = extract_structured_fields(text, "PO")
    assert got.get("customerName") == "北京优向国际能源装备有限公司"
    assert got.get("customerPoNo") == "PO-2026-0008"
    assert got.get("delivery_date") == "2026-06-15"
    assert got.get("material_code") == "S01P019430"
    assert got.get("line_qty") == "12"
    assert got.get("taxPrice") == "2.50"


def test_extract_prefers_real_chinese_customer_and_delivery_address():
    text = (
        "Purchase Order\n"
        "Global-set Valve Components Jiangsu Co., LTD\n"
        "格鲁赛特阀门配件江苏有限公司\n"
        "Delivery Address:\n"
        "Yao Lane Paragraph,122 Highway,Picheng Town Danyang City,Jiangsu Province (212300)\n"
        "江苏省丹阳市埤城镇122省道尧巷段（212300）\n"
        "Order No.: POGSVC2600205\n"
        "Material Code: SOGEYC2600\n"
        "Qty: 5000\n"
    )

    got = extract_structured_fields(text, "PO")

    assert got["customerName"] == "格鲁赛特阀门配件江苏有限公司"
    assert got["deliveryAddr"] == "江苏省丹阳市埤城镇122省道尧巷段（212300）"


def test_extract_keeps_english_when_no_real_chinese_text_exists():
    text = (
        "Purchase Order\n"
        "Buyer: Global-set Valve Components Jiangsu Co., LTD\n"
        "Delivery Address: Yao Lane Paragraph,122 Highway,Picheng Town Danyang City,Jiangsu Province (212300)\n"
        "Order No.: POGSVC2600205\n"
    )

    got = extract_structured_fields(text, "PO")

    assert got["customerName"] == "Global-set Valve Components Jiangsu Co., LTD"
    assert got["deliveryAddr"].startswith("Yao Lane Paragraph")


def test_extract_sap_srm_po_layout_without_llm():
    text = (
        "订单号 4501825923\n"
        "供应商名称 浙江英科弹簧科技有限公司 采购商名称 苏州纽威阀门股份有限公司\n"
        "1010012845 波形弹簧,84x72x1x5.8-D,INC X750 UNSN07750\n"
        "T04037 96 12.10H 0.0000\n"
        "10010231818碟形弹簧,B25x12.2x0.9,INC X750[GB/T1972,]UNSN07750\n"
        "T222644 3.37[GB/T1972,] A 0.0000\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert got["supplier_name"] == "浙江英科弹簧科技有限公司"
    assert got["buyer_name"] == "苏州纽威阀门股份有限公司"
    assert got["order_no"] == "4501825923"
    assert rows[0]["line_no"] == "10"
    assert rows[0]["inventory_code"] == "10012845"
    assert rows[0]["drawing_number"] == "T04037"
    assert rows[0]["quantity"] == "96"
    assert rows[1]["line_no"] == "100"
    assert rows[1]["inventory_code"] == "10231818"


def test_extract_sap_srm_po_multiline_metric_rows():
    text = (
        "30011691209 Wave spring,65x55x1x7.5,INC718-NC\n"
        "T02809 12 6.24 solution treated and aged 32-40HRC\n"
        "F 0.0000\n"
        "31011691209 Wave spring,65x55x1x7.5,INC718-NC\n"
        "T02809 4 6.24 solution treated and aged 32-40HRC\n"
        "F 0.0000\n"
        "2026/2/3 footer text\n"
        "37012745394 Disc spring,B50x25.4x2,INC X750\n"
        "T2226442 17.11[GB/T1972,] A 0.0200\n"
        "Total net weight: 7.0980\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert [row["line_no"] for row in rows] == ["300", "310", "370"]
    assert rows[0]["quantity"] == "12"
    assert rows[0]["unit"] == "F"
    assert rows[1]["quantity"] == "4"
    assert rows[2]["drawing_number"] == "T222644"
    assert rows[2]["quantity"] == "2"
    assert rows[2]["unit"] == "A"
    assert rows[2]["line_amount_excl_tax"] == "0.0200"


def test_extract_global_set_pipe_po_layout():
    text = (
        "Global-set Valve Components Jiangsu Co., LTD Address: Yao Lane Paragraph,122 Highway,"
        "Picheng Town Danyang City,Jiangsu Province (212300)\n"
        "Order No. :POGSVC2600205\n"
        "Issue Date : 6 / 2\n"
        "Fax: 0511-86322635 - 2026/3/6\n"
        "Item | Part No | Drawing No | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | SOGEYC2600 | sooson00s | 13.5x27.3 X-750 | 5000 | 4] 2026//27 49 24500\n"
        "2 | SOGEYC2601 | sooson00t | 14.5x28.3 X-750 | 2000 | 5.1 | 10200 | 2026/3/27\n"
        "3 | SOGEYC2602 | sooson00u | 15.5x29.3 X-750 | 1000 | 5.2 | 5200 | 2026/3/27\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert got["customerName"] == "Global-set Valve Components Jiangsu Co., LTD"
    assert got["customerPoNo"] == "POGSVC2600205"
    assert got["doc_date"] == "2026-03-06"
    assert got["delivery_date"] == "2026-03-27"
    assert got["currency"] == "CNY"
    assert got["deliveryAddr"].startswith("Yao Lane Paragraph")
    assert len(rows) == 3
    assert rows[0]["inventory_code"] == "SOGEYC2600"
    assert rows[0]["productSpec"] == "13.5x27.3"
    assert rows[0]["ph"] == "X-750"
    assert rows[0]["quantity"] == "5000"
    assert rows[0]["unit_price_excl_tax"] == "4.9"
    assert rows[0]["line_amount_excl_tax"] == "24500"
    assert rows[0]["delivery_date"] == "2026-03-27"


def test_extract_global_set_uses_code_column_as_material_code():
    text = (
        "Global-set Valve Components Jiangsu Co., LTD\n"
        "Order No.: POGSVC2600205\n"
        "Issue Date: 2026/3/6\n"
        "Item | Part No | Code | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | SOGEYC2600 | 020800003 | 13.5x27.3 x-750 | 5000 | 4.9 | 24500 | 2026/3/27\n"
        "2 | SOGSVC2600 | 020800004 | 11.5x23.5 X-750 | 5000 | 3 | 15000 | 2026/3/27\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert [row["inventory_code"] for row in rows] == ["020800003", "020800004"]
    assert rows[0].get("name", "") == ""
    assert rows[0].get("customerMaterialNo", "") == ""
    assert rows[0]["productSpec"] == "13.5x27.3"
    assert rows[0]["ph"] == "X-750"
    assert rows[0]["quantity"] == "5000"


def test_extract_global_set_uses_numeric_code_column_when_header_is_incomplete():
    text = (
        "Global-set Valve Components Jiangsu Co., LTD\n"
        "Order No.: POGSVC2600205\n"
        "Issue Date: 2026/3/6\n"
        "SN | Order No | No | Spec | Qty | Unit Price | Amount | Delivery Date\n"
        "1 | SOGEYC2600 | 020800003 | 13.5x27.3 x-750 | 5000 | 4.9 | 24500 | 2026/3/27\n"
        "2 | SOGSVC2600 | 020800004 | 11.5x23.5 X-750 | 5000 | 3 | 15000 | 2026/3/27\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert [row["inventory_code"] for row in rows] == ["020800003", "020800004"]
    assert rows[0].get("name", "") == ""
    assert rows[0]["productSpec"] == "13.5x27.3"
    assert rows[0]["ph"] == "X-750"


def test_extract_global_set_keeps_old_material_column_when_third_column_is_drawing_no():
    text = (
        "Global-set Valve Components Jiangsu Co., LTD\n"
        "Order No.: POGSVC2600205\n"
        "Issue Date: 2026/3/6\n"
        "Item | Part No | Drawing No | Specification | Quantity | Unit Price | Amount | Delivery Date\n"
        "1 | SOGEYC2600 | 020800003 | 13.5x27.3 x-750 | 5000 | 4.9 | 24500 | 2026/3/27\n"
    )
    got = extract_po_cn_layout_entities(text)
    import json

    rows = json.loads(got["line_items_json"])
    assert rows[0]["inventory_code"] == "SOGEYC2600"
    assert rows[0]["productSpec"] == "13.5x27.3"
    assert rows[0]["ph"] == "X-750"


def test_extract_inv_mixed_noise_invoice_token():
    text = (
        "Export packing list noise\n"
        "Commercial section INV-2026-X9-0002 endline\n"
        "Invoice Date 2026-06-11\n"
    )
    got = extract_structured_fields(text, "INV")
    assert got.get("invoice_no")
    assert "INV-2026" in got["invoice_no"]
    assert got.get("invoice_date") == "2026-06-11"
