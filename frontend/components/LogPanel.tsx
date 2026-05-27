"use client";

/**
 * 日志面板：展示 clientLogger 缓冲中的最近日志，支持折叠与清空。
 * 用途：让业务同学无需打开开发者工具即可复制错误上下文。
 */

import { useEffect, useMemo, useState } from "react";
import type { LogEntry } from "@/lib/client-logger";
import { clientLogger } from "@/lib/client-logger";

function levelColor(level: LogEntry["level"]): string {
  switch (level) {
    case "error":
      return "text-red-700 bg-red-50";
    case "warn":
      return "text-amber-800 bg-amber-50";
    case "debug":
      return "text-slate-600 bg-slate-100";
    default:
      return "text-sky-800 bg-sky-50";
  }
}

export interface LogPanelProps {
  /** 追加到根节点，便于嵌入抽屉等场景 */
  className?: string;
}

export function LogPanel({ className }: LogPanelProps) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    return clientLogger.subscribe((snapshot) => setEntries(snapshot));
  }, []);

  const text = useMemo(() => {
    return entries
      .map((e) => `${e.time} [${e.level}] ${e.message}${e.detail ? `\n${e.detail}` : ""}`)
      .join("\n\n");
  }, [entries]);

  return (
    <section
      className={[
        "rounded-2xl border border-slate-200/90 bg-white shadow-sm shadow-slate-200/30 ring-1 ring-slate-900/[0.03]",
        className ?? "",
      ].join(" ")}
    >
      <div className="flex items-center justify-between gap-3 border-b border-slate-200/90 px-4 py-3 sm:px-5">
        <div>
          <div className="text-base font-semibold text-slate-900">运行日志</div>
          <div className="text-sm text-slate-500">
            同步输出到浏览器控制台；共 {entries.length} 条（最多保留 {500} 条）
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            onClick={() => {
              clientLogger.clear();
              clientLogger.info("用户点击「清空日志」：已清空内存中的前端运行日志缓冲");
            }}
          >
            清空日志
          </button>
          <button
            type="button"
            className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
            onClick={() => {
              void navigator.clipboard.writeText(text).then(
                () => clientLogger.info("日志已复制到剪贴板"),
                () => clientLogger.warn("复制剪贴板失败：浏览器权限限制"),
              );
            }}
          >
            复制全部
          </button>
          <button
            type="button"
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "折叠" : "展开"}
          </button>
        </div>
      </div>

      {open ? (
        <div className="max-h-[320px] overflow-auto p-4 sm:p-5">
          {entries.length === 0 ? (
            <div className="px-2 py-6 text-center text-base text-slate-500">暂无日志</div>
          ) : (
            <ul className="space-y-2">
              {[...entries].reverse().map((e) => (
                <li key={e.id} className="rounded-lg border border-slate-100 bg-slate-50 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded px-2 py-0.5 text-sm font-semibold ${levelColor(e.level)}`}>
                      {e.level.toUpperCase()}
                    </span>
                    <span className="text-sm text-slate-500">{e.time}</span>
                  </div>
                  <div className="mt-2 text-base text-slate-900">{e.message}</div>
                  {e.detail ? (
                    <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white p-2 text-sm text-slate-700 ring-1 ring-slate-200">
                      {e.detail}
                    </pre>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </section>
  );
}
