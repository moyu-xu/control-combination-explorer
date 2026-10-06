import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CacheHelpPanel, Modal } from "./App";
import type { CacheStatus } from "./types";

const status: CacheStatus = {
  state: "normal",
  estimated_total_bytes: 300,
  protected_bytes: 100,
  historical_bytes: 200,
  reclaimable_bytes: 150,
  disk_bytes: 80,
  history_budget_bytes: 512 * 1024 * 1024,
  retention_seconds: 3600,
  sweep_seconds: 60,
  lease_seconds: 90,
  counts: { datasets: 1, analysis_jobs: 2, diagnostic_jobs: 3 },
  reclaimable_resources: 2,
  last_cleanup_at: null,
  last_reclaimed_bytes: 0,
  last_reclaimed_resources: 0,
  total_reclaimed_bytes: 0,
  pending_retired_datasets: 0
};

const t = (key: string) => key;

describe("CacheHelpPanel", () => {
  it("shows resource status and exposes refresh and cleanup actions", () => {
    const refresh = vi.fn();
    const cleanup = vi.fn();
    render(<CacheHelpPanel status={status} busy={false} error="" language="zh" t={t as never} cleanupButtonRef={createRef()} refresh={refresh} requestCleanup={cleanup} />);
    fireEvent.click(screen.getByText("cacheStorage"));
    expect(screen.getByText("1 / 2 / 3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "cacheRefresh" }));
    fireEvent.click(screen.getByRole("button", { name: "cacheCleanNow" }));
    expect(refresh).toHaveBeenCalledOnce();
    expect(cleanup).toHaveBeenCalledOnce();
  });

  it("disables cleanup and explains when no history is reclaimable", () => {
    render(<CacheHelpPanel status={{ ...status, reclaimable_resources: 0, reclaimable_bytes: 0 }} busy={false} error="" language="zh" t={t as never} cleanupButtonRef={createRef()} refresh={vi.fn()} requestCleanup={vi.fn()} />);
    fireEvent.click(screen.getByText("cacheStorage"));
    expect(screen.getByText(/cacheNothing/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "cacheCleanNow" })).toBeDisabled();
  });

  it("provides a retry action for specific cache errors", () => {
    const refresh = vi.fn();
    render(<CacheHelpPanel status={status} busy={false} error="Unable to read cache status" language="en" t={t as never} cleanupButtonRef={createRef()} refresh={refresh} requestCleanup={vi.fn()} />);
    fireEvent.click(screen.getByText("cacheStorage"));
    expect(screen.getByRole("alert")).toHaveTextContent("Unable to read cache status");
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    expect(refresh).toHaveBeenCalledOnce();
  });
});

describe("Modal keyboard behavior", () => {
  it("focuses the primary action and closes on Escape", () => {
    const close = vi.fn();
    render(<Modal title="Confirm" close={close}><button data-autofocus>Proceed</button></Modal>);
    expect(screen.getByRole("button", { name: "Proceed" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
  });
});

