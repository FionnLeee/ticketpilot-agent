import clsx from "clsx";
import { AlertTriangle, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

export function StatusPill({ value }: { value: string | null }) {
  const label = value?.replaceAll("_", " ") ?? "UNCLASSIFIED";
  return <span className={clsx("status-pill", `status-${value?.toLowerCase()}`)}>{label}</span>;
}

export function LoadingPanel({ label = "正在读取业务事实" }: { label?: string }) {
  return (
    <div className="state-panel" aria-live="polite">
      <RefreshCw size={18} className="spin" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorPanel({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state-panel state-error" role="alert">
      <AlertTriangle size={19} />
      <div>
        <strong>数据没有到达控制台</strong>
        <p>{error.message}。确认 FastAPI 已启动且演示 token 有效。</p>
      </div>
      {onRetry ? (
        <button className="button button-quiet" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export function EmptyPanel({ children }: { children: ReactNode }) {
  return <div className="state-panel state-empty">{children}</div>;
}

export function SectionHeading({
  eyebrow,
  title,
  aside,
}: {
  eyebrow: string;
  title: string;
  aside?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      {aside}
    </div>
  );
}
