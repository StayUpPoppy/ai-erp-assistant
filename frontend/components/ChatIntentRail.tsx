"use client";

/**
 * 聊天区左侧「意图分层」：与主区域同高；仅中间意图列表可纵向滚动（滚轮/触控），顶栏与底栏固定。
 */

import type { FC } from "react";

export type ChatIntentId =
  | "erp_professional"
  | "kb_general"
  | "doc_parse"
  | "doc_to_erp"
  | "master_data_align"
  | "report_assist"
  | "smart_audit_pending"
  | "contract_risk_pending"
  | "proc_trace_pending"
  | "price_alert_pending"
  | "capacity_risk_pending"
  | "three_way_match_pending"
  | "kb_erp_blend_pending"
  | "ops_exception_pending";

export type IntentImplementation = "live" | "placeholder" | "pending";

export interface ChatIntentDefinition {
  id: ChatIntentId;
  label: string;
  sourceLine: string;
  edition: "professional" | "general" | "workflow";
  implementation: IntentImplementation;
}

export const CHAT_INTENT_DEFINITIONS: ChatIntentDefinition[] = [
  {
    id: "kb_general",
    label: "通用问答",
    sourceLine: "公司制度、常见说明（建设中，暂不接后台）",
    edition: "general",
    implementation: "placeholder",
  },
  {
    id: "erp_professional",
    label: "问 ERP 里的数据",
    sourceLine: "查供应商、物料、仓库、订单等（先写「查什么」再写关键字）",
    edition: "professional",
    implementation: "live",
  },
  {
    id: "doc_parse",
    label: "文档解析",
    sourceLine: "上传 PDF/Word/图片，自动读文字和表格",
    edition: "workflow",
    implementation: "live",
  },
  {
    id: "doc_to_erp",
    label: "报表入库",
    sourceLine: "看进度、补信息、生成草稿对接入库",
    edition: "workflow",
    implementation: "live",
  },
  {
    id: "master_data_align",
    label: "核对编码",
    sourceLine: "单据编码与系统编码对照（建设中）",
    edition: "workflow",
    implementation: "placeholder",
  },
  {
    id: "report_assist",
    label: "报表与数字",
    sourceLine: "报表、指标问答（建设中）",
    edition: "general",
    implementation: "placeholder",
  },
  {
    id: "smart_audit_pending",
    label: "智能审单",
    sourceLine: "按规则标出单据风险（规划中）",
    edition: "workflow",
    implementation: "pending",
  },
  {
    id: "contract_risk_pending",
    label: "合同条款助手",
    sourceLine: "对照模板看条款差异（规划中）",
    edition: "general",
    implementation: "pending",
  },
  {
    id: "proc_trace_pending",
    label: "流程追溯",
    sourceLine: "从请购到对账一串看清卡在哪（规划中）",
    edition: "workflow",
    implementation: "pending",
  },
  {
    id: "price_alert_pending",
    label: "价差与采购提醒",
    sourceLine: "多供应商报价比对、超阈值提醒（规划中）",
    edition: "general",
    implementation: "pending",
  },
  {
    id: "capacity_risk_pending",
    label: "产能与交期预警",
    sourceLine: "提示可能延期、便于提前协调（规划中）",
    edition: "workflow",
    implementation: "pending",
  },
  {
    id: "three_way_match_pending",
    label: "三单匹配",
    sourceLine: "订单、入库、发票自动核对（规划中）",
    edition: "workflow",
    implementation: "pending",
  },
  {
    id: "kb_erp_blend_pending",
    label: "制度 + 实时数据",
    sourceLine: "制度说明和系统实时数据一起答（规划中）",
    edition: "general",
    implementation: "pending",
  },
  {
    id: "ops_exception_pending",
    label: "业务异常收件箱",
    sourceLine: "超期、缺料等异常集中查看（规划中）",
    edition: "workflow",
    implementation: "pending",
  },
];

const INTENT_BY_ID: Record<ChatIntentId, ChatIntentDefinition> = CHAT_INTENT_DEFINITIONS.reduce(
  (acc, d) => {
    acc[d.id] = d;
    return acc;
  },
  {} as Record<ChatIntentId, ChatIntentDefinition>,
);

export function getChatIntentDefinition(id: ChatIntentId): ChatIntentDefinition {
  return INTENT_BY_ID[id] ?? CHAT_INTENT_DEFINITIONS[1];
}

/** 主意图提示条应出现在哪块功能区上方 */
export type IntentBannerSlot = "chat" | "upload" | "workflow";

export function intentBannerSlot(id: ChatIntentId): IntentBannerSlot {
  if (id === "doc_parse") return "upload";
  if (id === "doc_to_erp" || id === "master_data_align") return "workflow";
  return "chat";
}

const RAIL_GROUPS: { title: string; ids: ChatIntentId[] }[] = [
  { title: "问答", ids: ["kb_general", "erp_professional"] },
  { title: "解析与入库", ids: ["doc_parse", "doc_to_erp"] },
  { title: "流程与主数据", ids: ["master_data_align", "report_assist"] },
  {
    title: "规划中（待定）",
    ids: [
      "smart_audit_pending",
      "contract_risk_pending",
      "proc_trace_pending",
      "price_alert_pending",
      "capacity_risk_pending",
      "three_way_match_pending",
      "kb_erp_blend_pending",
      "ops_exception_pending",
    ],
  },
];

function editionBadge(edition: ChatIntentDefinition["edition"]): { text: string; className: string } {
  switch (edition) {
    case "professional":
      return { text: "专业", className: "bg-violet-50 text-violet-800 ring-violet-200/80" };
    case "general":
      return { text: "通用", className: "bg-amber-50 text-amber-900 ring-amber-200/80" };
    default:
      return { text: "流程", className: "bg-slate-100 text-slate-700 ring-slate-200/80" };
  }
}

function implementationBadge(impl: IntentImplementation): { text: string; className: string } {
  switch (impl) {
    case "live":
      return { text: "可用", className: "bg-emerald-100 text-emerald-900 ring-emerald-200/80" };
    case "placeholder":
      return { text: "建设中", className: "bg-slate-200/90 text-slate-800 ring-slate-300/80" };
    case "pending":
      return { text: "规划中", className: "bg-sky-100 text-sky-900 ring-sky-200/80" };
  }
}

function railPrimaryBadge(def: ChatIntentDefinition): { text: string; className: string } {
  if (def.implementation === "pending") {
    return { text: "待定", className: "bg-sky-100 text-sky-900 ring-sky-200/80" };
  }
  return editionBadge(def.edition);
}

function editionAccent(edition: ChatIntentDefinition["edition"]): string {
  switch (edition) {
    case "professional":
      return "border-violet-400/90 from-violet-50/90 to-white";
    case "general":
      return "border-amber-300/90 from-amber-50/80 to-white";
    default:
      return "border-sky-300/90 from-sky-50/70 to-white";
  }
}

export interface CurrentIntentBannerProps {
  intentId: ChatIntentId;
  /** 附加 class，例如 mb-0 用于贴紧下方卡片 */
  className?: string;
}

/** 当前选中意图：顶栏下提示条 */
export const CurrentIntentBanner: FC<CurrentIntentBannerProps> = ({ intentId, className }) => {
  const def = getChatIntentDefinition(intentId);
  const edition = editionBadge(def.edition);
  const impl = implementationBadge(def.implementation);
  const accent = editionAccent(def.edition);
  return (
    <div
      className={[
        "mb-0 w-full rounded-xl border bg-gradient-to-br px-3 py-2.5 shadow-sm",
        accent,
        className ?? "",
      ].join(" ")}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">当前功能</div>
          <div className="mt-0.5 text-sm font-semibold tracking-tight text-slate-900">{def.label}</div>
          <p className="mt-1 text-xs leading-snug text-slate-600">{def.sourceLine}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span
            className={[
              "rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset",
              edition.className,
            ].join(" ")}
          >
            {edition.text}
          </span>
          <span
            className={[
              "rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset",
              impl.className,
            ].join(" ")}
          >
            {impl.text}
          </span>
        </div>
      </div>
    </div>
  );
};

export interface ChatIntentRailProps {
  value: ChatIntentId;
  onChange: (id: ChatIntentId) => void;
  className?: string;
}

export const ChatIntentRail: FC<ChatIntentRailProps> = ({ value, onChange, className }) => {
  return (
    <nav
      aria-label="功能分区"
      className={[
        "flex min-h-0 w-[13rem] shrink-0 flex-col overflow-hidden border-r border-slate-200/90 bg-gradient-to-b from-slate-50 via-slate-50 to-slate-100/80 sm:w-[14rem]",
        className ?? "",
      ].join(" ")}
    >
      <div className="shrink-0 border-b border-slate-200/80 px-2 py-2 sm:px-2.5">
        <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500">功能分区</div>
        <p className="mt-1 text-[10px] leading-tight text-slate-600">
          点一项切换；文件拖右侧灰区或左下角回形针。中间分区列表可滚轮上下滑动。
        </p>
      </div>

      <div
        className={[
          "min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-y-contain px-0.5",
          "[scrollbar-width:thin]",
          "[scrollbar-color:rgb(203_213_225)_transparent]",
        ].join(" ")}
      >
        <div className="flex flex-col pb-1">
          {RAIL_GROUPS.map((group) => (
            <div
              key={group.title}
              className="shrink-0 border-b border-slate-200/60 last:border-b-0"
            >
              <div className="bg-slate-100/60 px-2 py-1 text-[9px] font-semibold uppercase tracking-wide text-slate-500">
                {group.title}
              </div>
              <div className="flex flex-col gap-0.5 px-1.5 py-1">
                {group.ids.map((id) => {
                  const def = INTENT_BY_ID[id];
                  const active = value === id;
                  const primary = railPrimaryBadge(def);
                  return (
                    <button
                      key={id}
                      type="button"
                      onClick={() => onChange(id)}
                      className={[
                        "group relative flex w-full min-h-[3rem] flex-col justify-center rounded-md px-1.5 py-1.5 text-left transition",
                        active
                          ? "border border-sky-200/80 bg-white shadow-sm ring-1 ring-sky-200/60"
                          : "border border-transparent hover:border-slate-200 hover:bg-white/70",
                      ].join(" ")}
                    >
                      {active ? (
                        <span
                          className="absolute bottom-1.5 left-0 top-1.5 w-0.5 rounded-full bg-sky-500"
                          aria-hidden
                        />
                      ) : null}
                      <div className="min-w-0 pl-1">
                        <div className="flex flex-wrap items-center gap-x-1 gap-y-0">
                          <span
                            className={[
                              "text-[11px] font-semibold leading-tight",
                              active ? "text-sky-950" : "text-slate-800 group-hover:text-slate-950",
                            ].join(" ")}
                          >
                            {def.label}
                          </span>
                          <span
                            className={[
                              "rounded px-1 py-px text-[8px] font-semibold ring-1 ring-inset",
                              primary.className,
                            ].join(" ")}
                          >
                            {primary.text}
                          </span>
                        </div>
                        <p className="mt-0.5 line-clamp-3 text-[9px] leading-tight text-slate-500">{def.sourceLine}</p>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="shrink-0 border-t border-slate-200/80 bg-slate-50/90 px-2 py-1.5 text-[9px] leading-tight text-slate-500">
        意图见上条；对话在中，进度在下。
      </div>
    </nav>
  );
};

export function isErpProfessionalIntent(id: ChatIntentId): boolean {
  return id === "erp_professional";
}
