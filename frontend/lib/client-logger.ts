/**
 * 浏览器端统一日志模块。
 *
 * 设计目的：
 * 1) 满足「可观测」：关键用户操作、API 请求、轮询状态变化都有记录；
 * 2) 满足「可排障」：同时输出到控制台 + 页面内日志面板，便于非技术人员截图反馈；
 * 3) 注意：浏览器日志不应包含密码、token 等敏感信息；详情字段应脱敏后再写入。
 */

export type LogLevel = "debug" | "info" | "warn" | "error";

/** 单条日志结构：用于 UI 列表渲染与导出（后续可接远端日志） */
export interface LogEntry {
  id: string;
  time: string;
  level: LogLevel;
  message: string;
  /** 可选：JSON 字符串或短文本，过长会在入队时截断 */
  detail?: string;
}

const MAX_ENTRIES = 500;

/** Error 的 enumerable 属性为空，JSON.stringify 会变成 {}，排障时看不到 message。 */
function serializeDetail(detail: unknown): unknown {
  if (detail instanceof Error) {
    return {
      name: detail.name,
      message: detail.message,
      stack: detail.stack?.slice(0, 4000),
    };
  }
  return detail;
}

function safeStringify(value: unknown, maxLen: number): string {
  try {
    const s = JSON.stringify(value, null, 2);
    if (s.length <= maxLen) return s;
    return `${s.slice(0, maxLen)}\n…(已截断，总长度 ${s.length})`;
  } catch {
    return String(value).slice(0, maxLen);
  }
}

/**
 * ClientLogger：单例订阅模式。
 * - log：写入内存环形缓冲、打印控制台、通知所有订阅者；
 * - subscribe：React 组件挂载时订阅，卸载时取消。
 */
class ClientLogger {
  private entries: LogEntry[] = [];
  private listeners = new Set<(snapshot: LogEntry[]) => void>();

  subscribe(listener: (snapshot: LogEntry[]) => void): () => void {
    this.listeners.add(listener);
    listener([...this.entries]);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    const snapshot = [...this.entries];
    this.listeners.forEach((fn) => {
      try {
        fn(snapshot);
      } catch (e) {
        console.error("[助手前端] 日志订阅回调异常", e);
      }
    });
  }

  /**
   * 记录一条日志。
   * @param level 级别
   * @param message 人类可读简述（建议中文）
   * @param detail 可选附加信息（对象会被 JSON 化并截断）
   */
  log(level: LogLevel, message: string, detail?: unknown): void {
    const normalized = serializeDetail(detail);
    const detailStr =
      normalized === undefined
        ? undefined
        : typeof normalized === "string"
          ? normalized.slice(0, 8000)
          : safeStringify(normalized, 8000);

    const entry: LogEntry = {
      id:
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      time: new Date().toISOString(),
      level,
      message,
      detail: detailStr,
    };

    this.entries = [...this.entries, entry].slice(-MAX_ENTRIES);

    const prefix = `[助手前端][${level.toUpperCase()}]`;
    if (level === "error") {
      console.error(prefix, message, detail ?? "");
    } else if (level === "warn") {
      console.warn(prefix, message, detail ?? "");
    } else {
      console.log(prefix, message, detail ?? "");
    }

    this.emit();
  }

  debug(message: string, detail?: unknown): void {
    this.log("debug", message, detail);
  }

  info(message: string, detail?: unknown): void {
    this.log("info", message, detail);
  }

  warn(message: string, detail?: unknown): void {
    this.log("warn", message, detail);
  }

  error(message: string, detail?: unknown): void {
    this.log("error", message, detail);
  }

  /**
   * 清空内存中的日志缓冲（仅影响页面与订阅者，不影响已发送到远端日志系统的数据——当前未接远端）。
   * 用于用户或测试在排障后重置视图。
   */
  clear(): void {
    this.entries = [];
    this.emit();
    console.log("[助手前端][INFO] 日志缓冲已清空");
  }
}

/** 全应用共享的前端日志单例 */
export const clientLogger = new ClientLogger();
