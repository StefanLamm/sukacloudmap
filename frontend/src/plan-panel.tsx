import { VStack } from "@astryxdesign/core/VStack";
import { HStack } from "@astryxdesign/core/HStack";
import { Grid } from "@astryxdesign/core/Grid";
import { Section } from "@astryxdesign/core/Section";
import { Text } from "@astryxdesign/core/Text";
import { Heading } from "@astryxdesign/core/Heading";
import { Link } from "@astryxdesign/core/Link";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHeaderCell,
  TableCell,
} from "@astryxdesign/core/Table";
import { Action, Input, Select } from "./ui";
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Download,
  History,
  Play,
  RefreshCw,
  Route,
  Save,
  Square,
  Truck,
} from "lucide-react";
import { api } from "./api";
import { DayData } from "./demand-panel";
import { ErrorMessage, Spinner } from "./ui";
import { Pagination, PageSize, pageIndex } from "./pagination";
import { LocationMap } from "./location-map";
import { buildPlanMap, roadSegments, routeColors } from "./plan-map";
import {
  fleetSummary,
  optimizationLabel,
  optimizationStop,
  roadReferenceSummary,
  beijingReferenceTime,
  deepPlanningStages,
  planningStageIndex,
  planningProgressDetails,
  progressFee,
  deepValidationLabel,
} from "./plan-metrics";
import type { PlanningProgress, FinalRoadValidation } from "./plan-metrics";
import { confirmDiscard } from "./unsaved";
type Task = {
  id: string;
  code: string;
  name: string;
  pieces: number;
  warehouse_id: string;
  common_unit_price: string | null;
  longitude?: string;
  latitude?: string;
  crs?: string;
};
type Vehicle = {
  id: string;
  plate?: string;
  max_pieces: number;
  warehouse_id: string;
  day_warehouse_id?: string;
  available: boolean;
  day_available?: boolean;
};
type Stop = {
  task_id: string;
  arrival_at: string;
  unload_start_at: string;
  departure_at: string;
  wait_seconds: number;
  pieces: number;
};
type Segment = {
  received_at?: number;
  time_status?: string;
  source: string;
  verified: boolean;
  crs: string;
  polyline: number[][];
  meters: number;
  seconds: number;
  evidence_hash: string;
  request?: {
    coordinate_policy?: string;
  };
};
type RouteRow = {
  vehicle_id: string;
  plate?: string;
  task_ids: string[];
  pieces: number;
  meters: number;
  fee: string | null;
  regional_surcharge?: string;
  road_verified: boolean;
  stops: Stop[];
  segments: Segment[];
  errors: {
    message: string;
  }[];
};
type Result = {
  routes: RouteRow[];
  common: {
    task_id: string;
    pieces: number;
    fee: string | null;
    reason: string;
  }[];
  unassigned: {
    task_id: string;
    pieces: number;
    reason: string;
  }[];
  total_fee: string;
  fees_complete: boolean;
  comparison?: {
    delta: string | null;
    previous_total: string;
    current_total?: string;
    comparable: boolean;
    input_changed?: boolean;
    previous_dedicated?: string;
    current_dedicated?: string;
    previous_common?: string;
    current_common?: string;
  };
  confirmable: boolean;
  road_source: string;
  check: {
    valid: boolean;
    task_count: number;
    total_pieces: number;
    used_vehicles: number;
    dedicated_fee: string;
    common_fee: string;
    issues: {
      code: string;
      message: string;
      task_id?: string;
    }[];
  };
  algorithm: {
    automatic_dedicated_ids?: string[];
    mode: string;
    stop_reason: string;
    distinct_route_count?: number;
    elapsed_seconds?: number;
    all_common_baseline?: string | null;
    optimality_proven?: boolean;
    final_validation?: FinalRoadValidation;
    road_stats?: {
      calls: number;
      cache_hits: number;
    };
  };
};
type Input = {
  tasks: Record<string, unknown>[];
  stores?: Record<string, unknown>[];
  vehicles: Vehicle[];
  warehouses: Record<string, unknown>[];
  delivery_date: string;
};
type PlanMeta = {
  id: string;
  version: number;
  status: string;
  total_fee: string;
  input_changed: boolean;
  created_at: string;
  created_by: string;
  road_source: string;
  valid: boolean;
  snapshot_id: string;
  snapshot_version: number;
  content_hash: string;
  note: string;
  input_fingerprint: string;
  confirmable: boolean;
  blocking_issue_count?: number;
};
type Plan = PlanMeta & {
  input: Input;
  result: Result;
};
type Example = {
  id: string;
  input: Input;
  result: Result;
  notice: string;
};
type Job = {
  id: string;
  status: string;
  kind: string;
  budget_seconds?: number;
  cancel_requested: boolean;
  plan_id: string | null;
  error_message: string | null;
  progress?: PlanningProgress | null;
};
type Choice = {
  mode: string;
  order: number;
};
type Draft = {
  routes: {
    vehicle_id: string;
    task_ids: string[];
  }[];
  common_ids: string[];
  unassigned_ids: string[];
  auto_dedicated_ids: string[];
};
const colors = routeColors;
const text = (e: unknown) =>
  e instanceof Error ? e.message : "操作失败，请重试。";
const shortTime = (s: string) =>
  s?.replace("T", " ").replace("+08:00", "") ?? "—";
function taskList(input: Input): Task[] {
  const stores = new Map((input.stores ?? []).map((s) => [s.id, s]));
  return input.tasks.map((t) => {
    const s = stores.get(t.store_id) ?? {};
    return {
      ...s,
      ...t,
      id: String(t.store_id ?? t.id),
      code: String(s.code ?? t.code ?? t.id),
      name: String(s.name ?? t.name ?? t.id),
    } as Task;
  });
}
function initialChoices(result: Result): Record<string, Choice> {
  const out: Record<string, Choice> = {};
  for (const r of result.routes)
    r.task_ids.forEach(
      (id, i) => (out[id] = { mode: r.vehicle_id, order: i + 1 }),
    );
  for (const r of result.common) out[r.task_id] = { mode: "common", order: 1 };
  for (const r of result.unassigned)
    out[r.task_id] = { mode: "unassigned", order: 1 };
  for (const id of result.algorithm.automatic_dedicated_ids ?? [])
    if (out[id]) out[id] = { ...out[id], mode: "dedicated_auto" };
  return out;
}
function makeDraft(tasks: Task[], choices: Record<string, Choice>): Draft {
  const routes: Record<
    string,
    {
      id: string;
      order: number;
    }[]
  > = {};
  const out: Draft = {
    routes: [],
    common_ids: [],
    unassigned_ids: [],
    auto_dedicated_ids: [],
  };
  for (const t of tasks) {
    const c = choices[t.id] ?? { mode: "unassigned", order: 1 };
    if (c.mode === "common") out.common_ids.push(t.id);
    else if (c.mode === "unassigned") out.unassigned_ids.push(t.id);
    else if (c.mode === "dedicated_auto") out.auto_dedicated_ids.push(t.id);
    else (routes[c.mode] ??= []).push({ id: t.id, order: c.order });
  }
  out.routes = Object.entries(routes).map(([vehicle_id, rows]) => ({
    vehicle_id,
    task_ids: rows
      .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
      .map((r) => r.id),
  }));
  return out;
}
function JobProgress({ job, budget }: { job: Job; budget: number }) {
  const progress = job.progress ?? {};
  const phase =
    job.status === "QUEUED" ? -1 : planningStageIndex(progress.stage);
  const fee = progressFee(progress.best_fee);
  const manual = job.kind === "MANUAL";
  const heading = job.cancel_requested
    ? "正在停止，检查可保留的已验证结果"
    : job.status === "QUEUED"
      ? "任务等待执行"
      : manual
        ? "正在核算人工方案的里程、时序和费用"
        : `深度规划 · ${deepPlanningStages[phase] ?? deepPlanningStages[0]}`;
  return (
    <VStack gap={3} style={{ minWidth: 0 }}>
      <Text type="label" weight="semibold" hasTabularNumbers>
        {heading}
      </Text>
      {!manual && (
        <HStack
          as="ol"
          gap={2}
          wrap="wrap"
          style={{ listStyle: "none", padding: 0, margin: 0 }}
          aria-label="深度规划阶段"
        >
          {deepPlanningStages.map((label, i) => (
            <VStack
              as="li"
              gap={1}
              padding={2}
              key={label}
              aria-current={i === phase ? "step" : undefined}
              style={{
                borderRadius: "var(--radius-2)",
                background:
                  i === phase
                    ? "var(--color-accent-muted)"
                    : "var(--color-background-muted)",
                color:
                  i === phase
                    ? "var(--color-text-accent)"
                    : i < phase
                      ? "var(--color-text-success)"
                      : "var(--color-text-secondary)",
              }}
            >
              {label}
            </VStack>
          ))}
        </HStack>
      )}
      {!manual && progress.message && (
        <Text type="supporting" display="block">
          {progress.message}
        </Text>
      )}
      {!manual && planningProgressDetails(progress) && (
        <Text type="supporting" display="block">
          {planningProgressDetails(progress)}
        </Text>
      )}
      {!manual && fee !== null && (
        <Text type="large" weight="semibold" hasTabularNumbers>
          当前参考最低费用 ¥{fee}
          <Text type="supporting" display="block">
            道路复核后确认最终报价
          </Text>
        </Text>
      )}
      <Text type="supporting" display="block">
        可继续查看业务资料；优化计算上限 {job.budget_seconds ?? budget}{" "}
        秒，道路排队另计
      </Text>
      {job.cancel_requested && (
        <Text type="supporting" display="block">
          通过完整校验的结果才会保留为草稿；未完成核验的候选不会保存。
        </Text>
      )}
    </VStack>
  );
}
export function PlanPanel({
  day,
  groupId,
  demand,
  notify,
}: {
  day: string;
  groupId?: string;
  demand: DayData | null;
  notify: (s: string) => void;
}) {
  const [versions, setVersions] = useState<PlanMeta[]>([]);
  const [versionTotal, setVersionTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [historySize, setHistorySize] = useState<PageSize>(10);
  const historyGeneration = useRef(0);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [example, setExample] = useState<Example | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [budget, setBudget] = useState(300);
  const [cap, setCap] = useState<{
    live_calls_enabled: boolean;
    truck_key_configured: boolean;
    max_calls: number | null;
    qps?: number;
    road_source: string;
    route_mode?: string;
  } | null>(null);
  const drivingReference =
    cap?.route_mode === "DRIVING" && cap.road_source === "LIVE";
  const canRefreshRoads = Boolean(drivingReference && cap?.live_calls_enabled);
  const [optimizeRoutes, setOptimizeRoutes] = useState(false);
  const [refreshManual, setRefreshManual] = useState(true);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [note, setNote] = useState("");
  const [mapVehicle, setMapVehicle] = useState("all");
  const [routeMapId, setRouteMapId] = useState<string | null>(null);
  useEffect(() => {
    setMapVehicle("all");
    setRouteMapId(null);
  }, [plan?.id, example?.id]);
  const [resultQuery, setResultQuery] = useState("");
  const [resultPage, setResultPage] = useState(0);
  const [resultSize, setResultSize] = useState<PageSize>(10);
  const [issuePage, setIssuePage] = useState(0);
  const [issueSize, setIssueSize] = useState<PageSize>(10);
  const [sub, setSub] = useState<"routes" | "common" | "errors" | "map">(
    "routes",
  );
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  const [head, setHead] = useState(0);
  const selectionGeneration = useRef(0);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const active = job && ["QUEUED", "RUNNING"].includes(job.status);
  const current = example ?? plan;
  const result = current?.result;
  const tasks = useMemo(
    () => (current ? taskList(current.input) : []),
    [current],
  );
  const taskById = new Map(tasks.map((t) => [t.id, t]));
  // Background selection retains the token captured before history loading;
  // later user actions invalidate both the list and plan response.
  async function loadVersions(
    start = offset,
    open = false,
    size = historySize,
  ) {
    const initialSelection = selectionGeneration.current;
    const request = ++historyGeneration.current;
    const r = await api<{
      items: PlanMeta[];
      total: number;
      head_version: number;
    }>(
      `/planning/days/${day}/plans?offset=${start}&limit=${size}${groupId ? "&group_id=" + encodeURIComponent(groupId) : ""}`,
    );
    if (!alive.current || request !== historyGeneration.current) return;
    setVersions(r.items);
    setVersionTotal(r.total);
    setHead(r.head_version);
    if (
      open &&
      r.items[0] &&
      initialSelection === selectionGeneration.current &&
      !dirtyRef.current
    )
      await openPlan(r.items[0].id, true, initialSelection);
  }
  async function openPlan(
    id: string,
    force = false,
    initialSelection?: number,
  ) {
    if (
      initialSelection !== undefined &&
      (initialSelection !== selectionGeneration.current || dirtyRef.current)
    )
      return;
    if (
      !force &&
      dirtyRef.current &&
      !(await confirmDiscard(
        "当前人工选择尚未重算保存，切换版本会丢弃这些未提交选择。确定切换？",
      ))
    )
      return;
    const generation = initialSelection ?? ++selectionGeneration.current;
    let r: Plan;
    try {
      r = await api<Plan>("/planning/plans/" + id);
    } catch (e) {
      if (
        initialSelection !== undefined &&
        (!alive.current ||
          generation !== selectionGeneration.current ||
          dirtyRef.current)
      )
        return;
      throw e;
    }
    if (
      !alive.current ||
      generation !== selectionGeneration.current ||
      (initialSelection !== undefined && dirtyRef.current)
    )
      return;
    setPlan(r);
    setExample(null);
    setChoices(initialChoices(r.result));
    setEditing(false);
    setDirty(false);
    setPage(0);
    setQuery("");
    setResultPage(0);
  }
  useEffect(() => {
    api<typeof cap>("/planning/capabilities")
      .then(setCap)
      .catch((e) => setError(text(e)));
    loadVersions(0, true).catch((e) => setError(text(e)));
    api<{
      items: Job[];
    }>(
      "/planning/days/" +
        day +
        "/jobs" +
        (groupId ? "?group_id=" + encodeURIComponent(groupId) : ""),
    )
      .then((r) => {
        if (alive.current)
          setJob(
            r.items.find((j) => ["QUEUED", "RUNNING"].includes(j.status)) ??
              null,
          );
      })
      .catch((e) => setError(text(e)));
  }, []);
  useEffect(() => {
    if (!active || !job) return;
    let stopped = false;
    let timer: number | undefined;
    const id = job.id;
    async function poll() {
      try {
        const r = await api<Job>("/planning/jobs/" + id);
        if (stopped || !alive.current) return;
        if (!["QUEUED", "RUNNING"].includes(r.status)) {
          if (r.error_message) setError(r.error_message);
          if (r.plan_id) {
            await openPlan(r.plan_id, true);
            await loadVersions(0);
            setOffset(0);
            notify(
              r.status === "CANCELLED"
                ? "已取消，并保留停止前的结果草稿"
                : r.kind === "MANUAL"
                  ? "核算完成，已保存新方案版本"
                  : "新方案版本已保存",
            );
          }
          if (!stopped) setJob(r);
          return;
        }
        setJob(r);
      } catch (e) {
        if (!stopped) setError(text(e));
      }
      if (!stopped) timer = window.setTimeout(poll, 700);
    }
    timer = window.setTimeout(poll, 250);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [active, job?.id]);
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    const leave = (e: Event) => {
      if (dirtyRef.current)
        (e as CustomEvent<Promise<boolean>[]>).detail.push(
          confirmDiscard("离开会丢失尚未重算保存的人工选择。"),
        );
    };
    window.addEventListener("beforeunload", handler);
    window.addEventListener("suka-before-leave", leave);
    return () => {
      window.removeEventListener("beforeunload", handler);
      window.removeEventListener("suka-before-leave", leave);
    };
  }, []);
  async function start(kind: "OPTIMIZE" | "MANUAL") {
    const snapshot =
      kind === "MANUAL" ? plan?.snapshot_id : demand?.latest_snapshot?.id;
    if (!snapshot) return;
    setBusy(true);
    setError("");
    try {
      const r = await api<Job>("/planning/jobs", "POST", {
        snapshot_id: snapshot,
        ...(groupId ? { group_id: groupId } : {}),
        expected_head: head,
        kind,
        optimize_routes: kind === "MANUAL" && optimizeRoutes,
        ...(kind === "MANUAL" && canRefreshRoads
          ? { refresh_roads: refreshManual }
          : {}),
        budget_seconds: budget,
        call_limit: cap?.live_calls_enabled ? null : 0,
        seed: 0,
        note,
        ...(kind === "MANUAL"
          ? { draft: makeDraft(tasks, choices), parent_id: plan?.id }
          : {}),
      });
      setJob(r);
    } catch (e) {
      setError(text(e));
    } finally {
      setBusy(false);
    }
  }
  async function showExample(id: string) {
    if (
      dirtyRef.current &&
      !(await confirmDiscard(
        "当前人工选择尚未重算保存，切换样例会丢弃这些未提交选择。确定切换？",
      ))
    )
      return;
    setBusy(true);
    setError("");
    try {
      const generation = ++selectionGeneration.current;
      const r = await api<Example>("/planning/examples/" + id);
      if (!alive.current || generation !== selectionGeneration.current) return;
      setExample(r);
      setChoices(initialChoices(r.result));
      setEditing(false);
      setDirty(false);
      setPage(0);
      setResultPage(0);
      setSub("routes");
    } catch (e) {
      setError(text(e));
    } finally {
      setBusy(false);
    }
  }
  async function recalcExample() {
    if (!example) return;
    setBusy(true);
    setError("");
    try {
      const r = await api<Example>(
        "/planning/examples/" +
          example.id +
          "/recalculate" +
          (optimizeRoutes ? "?optimize_routes=true" : ""),
        "POST",
        makeDraft(tasks, choices),
      );
      setExample(r);
      setDirty(false);
      notify(
        r.result.check.valid
          ? "样例重算通过；固定道路不可用于执行"
          : "样例冲突已保留，请继续调整",
      );
    } catch (e) {
      setError(text(e));
    } finally {
      setBusy(false);
    }
  }
  async function versionAction(action: "confirm" | "restore") {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      const r = await api<Plan>(
        `/planning/plans/${plan.id}/${action}`,
        "POST",
        { expected_head: head, note },
      );
      setPlan(r);
      setExample(null);
      setEditing(false);
      setDirty(false);
      setChoices(initialChoices(r.result));
      await loadVersions(0);
      setOffset(0);
      notify(
        action === "confirm" ? "已创建人工确认版本" : "历史方案已恢复为新草稿",
      );
    } catch (e) {
      setError(text(e));
    } finally {
      setBusy(false);
    }
  }
  function touchEditor() {
    selectionGeneration.current++;
    dirtyRef.current = true;
    setDirty(true);
  }
  function change(id: string, mode: string) {
    setChoices((old) => ({
      ...old,
      [id]: {
        mode,
        order:
          Math.max(
            0,
            ...Object.values(old)
              .filter((c) => c.mode === mode)
              .map((c) => c.order),
          ) + 1,
      },
    }));
    touchEditor();
  }
  function move(id: string, direction: number) {
    const c = choices[id];
    const group = tasks
      .filter((t) => choices[t.id]?.mode === c.mode)
      .sort(
        (a, b) =>
          choices[a.id].order - choices[b.id].order || a.id.localeCompare(b.id),
      );
    const index = group.findIndex((t) => t.id === id);
    const next = group[index + direction];
    if (!next) return;
    [group[index], group[index + direction]] = [
      group[index + direction],
      group[index],
    ];
    setChoices((old) => {
      const out = { ...old };
      group.forEach((t, i) => (out[t.id] = { ...out[t.id], order: i + 1 }));
      return out;
    });
    touchEditor();
  }
  const currentDraft = useMemo(
    () => makeDraft(tasks, choices),
    [tasks, choices],
  );
  const taskOrders = useMemo(() => {
    const orders = new Map<string, number>();
    for (const r of currentDraft.routes)
      r.task_ids.forEach((id, i) => orders.set(id, i + 1));
    return orders;
  }, [currentDraft]);
  const filtered = tasks.filter((t) =>
    [t.code, t.name].some((v) => v.includes(query)),
  );
  const editorPage = pageIndex(page, filtered.length, pageSize);
  const visible = filtered.slice(
    editorPage * pageSize,
    editorPage * pageSize + pageSize,
  );
  const vehicles = current?.input.vehicles ?? [];
  const proof = result?.check;
  const fleet = fleetSummary(result?.routes ?? [], vehicles);
  const references = roadReferenceSummary(result?.routes ?? []);
  const blockingIssues =
    proof?.issues.filter((issue) => issue.code !== "DRIVING_REFERENCE") ?? [];
  const planStale = Boolean(
    plan &&
      (plan.input_changed ||
        (demand && plan.input_fingerprint !== demand.input_fingerprint)),
  );
  const filteredCommon = (result?.common ?? []).filter((r) => {
    const t = taskById.get(r.task_id);
    return (
      !resultQuery || Boolean(t && (t.code + t.name).includes(resultQuery))
    );
  });
  const commonPages = Math.ceil(filteredCommon.length / resultSize);
  const commonPage = Math.min(resultPage, Math.max(0, commonPages - 1));
  const unassignedPage = pageIndex(
    resultPage,
    result?.unassigned.length ?? 0,
    resultSize,
  );
  const conflictPage = pageIndex(
    issuePage,
    proof?.issues.length ?? 0,
    issueSize,
  );
  const mapped = buildPlanMap(
    current?.input.warehouses ?? [],
    taskById,
    result?.routes ?? [],
    (result?.common ?? []).map((row) => row.task_id),
    mapVehicle === "all" ? undefined : mapVehicle,
  );
  const inlineMap = routeMapId
    ? buildPlanMap(
        current?.input.warehouses ?? [],
        taskById,
        result?.routes ?? [],
        [],
        routeMapId,
      )
    : null;
  return (
    <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
      <Section
        padding={4}
        style={{
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          gap: "var(--spacing-4)",
        }}
      >
        <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
          <VStack gap={3} style={{ minWidth: 0 }}>
            <Text type="supporting" weight="semibold" color="accent">
              PLAN & REVIEW
            </Text>
            <Heading level={2}>生成配送方案</Heading>
            <Text type="supporting" as="p">
              自动多轮搜索、汇总线路并联合选优，经道路复核后保存找到的最低费用可行方案。
            </Text>
          </VStack>
          <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
            <VStack gap={2}>
              <label htmlFor="planning-time-budget">
                <Text type="label">优化计算时限（秒）</Text>
              </label>
              <Input
                id="planning-time-budget"
                aria-label="优化时限"
                type="number"
                min={1}
                max={24000}
                value={budget}
                onChange={(e) =>
                  setBudget(
                    Math.max(1, Math.min(24000, Number(e.target.value) || 1)),
                  )
                }
              />
            </VStack>
            <Section
              variant="muted"
              padding={3}
              style={{
                minWidth: 0,
                display: "flex",
                flexDirection: "column",
                gap: "var(--spacing-2)",
              }}
            >
              <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
                <Text type="body">高德算路调用次数</Text>
                <Text
                  type="label"
                  weight="semibold"
                  hasTabularNumbers
                  aria-label="高德算路调用上限"
                >
                  {cap?.live_calls_enabled && cap.truck_key_configured
                    ? cap.max_calls == null
                      ? "无限制"
                      : cap.max_calls + "次"
                    : "外部算路关闭"}
                </Text>
                <Text type="supporting" display="block">
                  每秒最多 {cap?.qps ?? 3} 次，可取消任务
                </Text>
              </VStack>
            </Section>
            <Action
              variant="primary"
              disabled={
                busy ||
                !!active ||
                !demand?.latest_snapshot ||
                demand.latest_snapshot.input_changed
              }
              onClick={() => void start("OPTIMIZE")}
            >
              <Play size={15} /> 开始规划
            </Action>
          </HStack>
          <Section
            variant="muted"
            padding={3}
            style={{
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              gap: "var(--spacing-2)",
            }}
          >
            <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
              <Text type="label" weight="semibold" hasTabularNumbers>
                自动深度规划
              </Text>
              <Text type="body">
                多轮多起点搜索 → 汇总线路 → 联合选优 → 道路复核与修复 → 保存方案
              </Text>
              <Text type="supporting" display="block">
                {canRefreshRoads
                  ? "自动调用高德复核选中道路并重新报价；按每秒限流排队，等待时间另计，支持取消。"
                  : cap?.road_source === "TEST_GRID"
                    ? "当前为隔离测试道路，只保存测试结果，不提供真实道路核验。"
                    : drivingReference
                      ? "当前入口未开放外部算路，将使用已有实路参考复核；不会标记为实时道路结果。"
                      : "按当前道路配置完成复核；道路未核验的专车方案不能确认执行。"}
              </Text>
            </VStack>
          </Section>
          <Text type="supporting" as="p">
            {demand?.latest_snapshot
              ? `输入快照 V${demand.latest_snapshot.version} · ${day}`
              : "请先导入需求并冻结输入快照。"}{" "}
            · 规划采用全部仓库的冻结输入
            {cap?.road_source === "TEST_GRID"
              ? " · 正在使用非地理模拟路网，仅供隔离测试；结果不能执行"
              : cap?.route_mode === "DRIVING"
                ? ` · 驾车参考规划：未核验货车限行；${cap.live_calls_enabled ? "按每秒限流排队生成道路轨迹，总调用次数不限。" : "本轮外部调用关闭，可查看已保存轨迹。"}`
                : !cap?.live_calls_enabled
                  ? " · 本轮真实调用关闭，无法核验的专车按共配配置回退或列入未分配。"
                  : ""}
          </Text>
        </VStack>
      </Section>
      <ErrorMessage text={error} />
      {active && (
        <HStack
          gap={3}
          wrap="wrap"
          align="center"
          style={{ minWidth: 0 }}
          role="status"
        >
          <Spinner />
          <JobProgress job={job!} budget={budget} />
          <Action
            variant="secondary"
            disabled={job?.cancel_requested}
            onClick={() => {
              api<Job>("/planning/jobs/" + job!.id + "/cancel", "POST")
                .then((r) =>
                  setJob((v) =>
                    v ? { ...v, cancel_requested: r.cancel_requested } : r,
                  ),
                )
                .catch((e) => setError(text(e)));
            }}
          >
            <Square size={13} />
            {job?.cancel_requested ? "正在停止" : "取消任务"}
          </Action>
        </HStack>
      )}
      <HStack gap={4} wrap="wrap" align="start" style={{ minWidth: 0 }}>
        <Section
          padding={4}
          style={{
            flex: "3 1 0",
            minWidth: "min(100%, calc(var(--spacing-10) * 15))",
            display: "flex",
            flexDirection: "column",
            gap: "var(--spacing-4)",
          }}
        >
          <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
            <HStack
              gap={3}
              wrap="wrap"
              align="center"
              justify="between"
              style={{ minWidth: 0 }}
            >
              <HStack
                gap={3}
                wrap="wrap"
                align="center"
                style={{ minWidth: 0 }}
              >
                <Route size={17} />
                <Heading level={2}>
                  {example
                    ? "固定道路演练"
                    : plan
                      ? `方案 V${plan.version}`
                      : "方案结果"}
                </Heading>
                {plan && !example && (
                  <Text type="body" color="accent">
                    {plan.status === "CONFIRMED" ? "已确认" : "草稿"}
                  </Text>
                )}
              </HStack>
              <HStack
                gap={3}
                wrap="wrap"
                align="center"
                style={{ minWidth: 0 }}
              >
                {current && (
                  <Action
                    variant="secondary"
                    disabled={busy || !!active}
                    onClick={() => {
                      selectionGeneration.current++;
                      setEditing(!editing);
                      setPage(0);
                    }}
                  >
                    <Truck size={14} />
                    {editing ? "收起人工调整" : "人工调整"}
                  </Action>
                )}
                {plan && !example && (
                  <Link
                    isStandalone
                    href={"/api/planning/plans/" + plan.id + "/export.xlsx"}
                  >
                    <HStack as="span" gap={2} vAlign="center">
                      <Download size={14} /> 导出 Excel
                    </HStack>
                  </Link>
                )}
              </HStack>
            </HStack>
            {!current && (
              <VStack
                gap={3}
                paddingBlock={6}
                align="center"
                style={{ minWidth: 0, textAlign: "center" }}
              >
                <Route size={32} />
                <Heading level={3}>方案将保存在这里</Heading>
                <Text type="body" as="p">
                  导入需求后开始规划，或使用右侧固定样例检查排线与人工调整。
                </Text>
              </VStack>
            )}
            {current && result && proof && (
              <>
                {!example && result.road_source === "DRIVING" && (
                  <Section
                    variant="muted"
                    padding={3}
                    style={{
                      minWidth: 0,
                      display: "flex",
                      flexDirection: "column",
                      gap: "var(--spacing-2)",
                    }}
                  >
                    <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
                      驾车参考方案：未核验货车限高、限重、限行及计划发车时刻，路线、预计到店和费用需人工核对，不能确认执行。
                    </VStack>
                  </Section>
                )}
                {!example &&
                  result.road_source === "DRIVING" &&
                  result.routes.some((r) =>
                    r.segments.some(
                      (s) =>
                        s.request?.coordinate_policy ===
                        "EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02",
                    ),
                  ) && (
                    <Text type="supporting" as="p">
                      部分位置采用坐标换算，原资料保留；请核对实际位置。
                    </Text>
                  )}
                {!example && result.road_source === "FIXTURE" && (
                  <Section
                    variant="muted"
                    padding={3}
                    style={{
                      minWidth: 0,
                      display: "flex",
                      flexDirection: "column",
                      gap: "var(--spacing-2)",
                    }}
                  >
                    <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
                      隔离测试：使用非地理模拟道路，里程、费用、时刻仅用于验收，不是实际承运或导航方案。
                    </VStack>
                  </Section>
                )}
                {example && (
                  <Section
                    variant="muted"
                    padding={3}
                    style={{
                      minWidth: 0,
                      display: "flex",
                      flexDirection: "column",
                      gap: "var(--spacing-2)",
                    }}
                  >
                    <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
                      固定道路测试 · {example.id} · 不写入业务数据 ·
                      不可确认执行
                      {plan && (
                        <Action
                          variant="ghost"
                          onClick={async () => {
                            if (
                              dirtyRef.current &&
                              !(await confirmDiscard(
                                "尚未重算的选择会丢失，确定返回业务方案？",
                              ))
                            )
                              return;
                            setExample(null);
                            setEditing(false);
                            setDirty(false);
                            setChoices(initialChoices(plan.result));
                          }}
                        >
                          返回业务方案
                        </Action>
                      )}
                    </VStack>
                  </Section>
                )}
                {!example && planStale && (
                  <Section
                    variant="muted"
                    padding={3}
                    style={{
                      minWidth: 0,
                      display: "flex",
                      flexDirection: "column",
                      gap: "var(--spacing-2)",
                    }}
                  >
                    <VStack gap={2} width="100%" style={{ minWidth: 0 }}>
                      输入已变化。本版本保留原始费用、时序与道路证据；请重新冻结输入后规划。
                    </VStack>
                  </Section>
                )}
                <Grid
                  columns={{ minWidth: 150, max: 5 }}
                  gap={4}
                  style={{ minWidth: 0 }}
                >
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body">
                      {result.road_source === "DRIVING"
                        ? "驾车参考费用"
                        : result.fees_complete !== false
                          ? "方案费用"
                          : "已核算金额（有未定费用）"}
                    </Text>
                    <Text type="large" weight="semibold" hasTabularNumbers>
                      ¥ {result.total_fee}
                    </Text>
                  </VStack>
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body">专车 / 共配费用</Text>
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      ¥ {proof.dedicated_fee} / ¥ {proof.common_fee}
                    </Text>
                  </VStack>
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body">任务 / 件数 / 用车</Text>
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      {proof.task_count} 票 · {proof.total_pieces} 件 ·{" "}
                      {proof.used_vehicles} 辆
                    </Text>
                  </VStack>
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body">专车总里程 / 件数装载率</Text>
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      {(fleet.meters / 1000).toFixed(2)} km ·{" "}
                      {fleet.loadedPercent === null
                        ? "—"
                        : fleet.loadedPercent.toFixed(1) + "%"}
                    </Text>
                    <Text type="supporting" display="block">
                      总支出优先，同费用下缩短里程
                    </Text>
                  </VStack>
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body">独立复核</Text>
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      {!blockingIssues.length
                        ? "分配校验通过"
                        : `${blockingIssues.length} 项待处理`}
                    </Text>
                    <Text type="supporting" display="block">
                      {result.road_source === "DRIVING"
                        ? "驾车参考，货车限行待核验"
                        : result.road_source === "FIXTURE"
                          ? "固定测试道路"
                          : result.road_source === "COMMON_ONLY"
                            ? "全共配方案"
                            : result.confirmable
                              ? "货车道路已核验"
                              : "专车道路待核验"}
                    </Text>
                  </VStack>
                </Grid>
                {result.comparison && (
                  <HStack
                    gap={3}
                    wrap="wrap"
                    align="center"
                    style={{ minWidth: 0 }}
                  >
                    {result.comparison.comparable ? (
                      <>
                        <Text type="label" weight="semibold" hasTabularNumbers>
                          核算前 ¥{result.comparison.previous_total} → 最新报价
                          ¥{result.comparison.current_total ?? result.total_fee}
                        </Text>
                        <Text type="body">
                          {Number(result.comparison.delta) > 0
                            ? "增加"
                            : Number(result.comparison.delta) < 0
                              ? "减少"
                              : "差额"}{" "}
                          ¥
                          {Math.abs(Number(result.comparison.delta)).toFixed(2)}
                        </Text>
                        {result.comparison.previous_dedicated !== undefined && (
                          <Text type="supporting" display="block">
                            专车 ¥{result.comparison.previous_dedicated} → ¥
                            {result.comparison.current_dedicated}；共配 ¥
                            {result.comparison.previous_common} → ¥
                            {result.comparison.current_common}
                          </Text>
                        )}
                      </>
                    ) : (
                      "存在未核算费用，处理后再比较调整差额。"
                    )}
                    {result.comparison.input_changed && (
                      <Text type="supporting" display="block">
                        本次费用对比包含仓库归属或其他规划输入变化，请结合快照查看。
                      </Text>
                    )}
                  </HStack>
                )}
                <VStack gap={3} style={{ minWidth: 0 }}>
                  {optimizationLabel(
                    result.algorithm.mode,
                    result.algorithm.optimality_proven,
                  )}{" "}
                  · {optimizationStop(result.algorithm.stop_reason)}
                  {!result.algorithm.optimality_proven &&
                    " · 未证明全局最低"}{" "}
                  {result.algorithm.distinct_route_count !== undefined
                    ? `· 已比较 ${result.algorithm.distinct_route_count} 条候选线路`
                    : ""}{" "}
                  {result.algorithm.elapsed_seconds !== undefined
                    ? `· ${result.algorithm.elapsed_seconds}s`
                    : ""}
                  {result.algorithm.all_common_baseline
                    ? ` · 全共配基线 ¥${result.algorithm.all_common_baseline}`
                    : ""}
                  {result.algorithm.road_stats
                    ? ` · 外部调用 ${result.algorithm.road_stats.calls} 次 / 缓存命中 ${result.algorithm.road_stats.cache_hits} 次`
                    : ""}
                </VStack>
                {result.algorithm.mode === "DAILY_DEEP_PORTFOLIO" && (
                  <Text type="body" as="p">
                    {deepValidationLabel(result.algorithm.final_validation)}
                  </Text>
                )}
                {references.count > 0 && (
                  <Text type="body" as="p">
                    {references.historical ? "历史实路参考" : "实路参考"} ·
                    采集时间（北京时间）：
                    {beijingReferenceTime(references.oldest)}
                    {references.newest !== references.oldest
                      ? " 至 " + beijingReferenceTime(references.newest)
                      : ""}
                    {references.missingTime ? " · 部分路段未记录时间" : ""}。
                    {canRefreshRoads
                      ? "需要更新路况时，可在核算中选择刷新道路。"
                      : "核算时使用当前入口的道路配置。"}
                  </Text>
                )}
                {editing && (
                  <Section
                    variant="muted"
                    padding={3}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "var(--spacing-4)",
                      minWidth: 0,
                    }}
                  >
                    <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
                      <HStack
                        gap={3}
                        wrap="wrap"
                        align="center"
                        justify="between"
                        style={{ minWidth: 0 }}
                      >
                        <VStack gap={3} style={{ minWidth: 0 }}>
                          <Heading level={3}>
                            人工调整配送方式、车辆和顺序
                          </Heading>
                          <Text type="body" as="p">
                            选择是否优化线路后点击“开始核算”，重新计算里程、到店时间和最新报价，保存新版本并与核算前报价对比。可直接选择车辆，或选择“专车（自动分车排线）”，由系统按货量、线路与费用分配车辆；共配及指定车辆保持人工选择。
                          </Text>
                        </VStack>
                        <Action
                          variant="primary"
                          disabled={busy || !!active}
                          onClick={() =>
                            void (example ? recalcExample() : start("MANUAL"))
                          }
                        >
                          <Play size={14} />{" "}
                          {busy || active ? "正在核算…" : "开始核算"}
                        </Action>
                      </HStack>
                      <Grid
                        columns={{ minWidth: 220, max: 2 }}
                        gap={4}
                        style={{ minWidth: 0 }}
                      >
                        <VStack gap={2} style={{ minWidth: 0 }}>
                          <label htmlFor="manual-road-reference">
                            <Text type="label" display="block">
                              道路核算方式
                            </Text>
                          </label>
                          <Select
                            id="manual-road-reference"
                            disabled={busy || !!active || !canRefreshRoads}
                            value={
                              canRefreshRoads && refreshManual
                                ? "refresh"
                                : "cached"
                            }
                            onChange={(e) => {
                              setRefreshManual(e.target.value === "refresh");
                              touchEditor();
                            }}
                          >
                            {canRefreshRoads && (
                              <option value="refresh">
                                刷新所选道路并重新核算
                              </option>
                            )}
                            <option value="cached">
                              {drivingReference
                                ? "使用已有实路参考核算"
                                : "按当前道路配置核算"}
                            </option>
                          </Select>
                        </VStack>
                        <VStack gap={2} style={{ minWidth: 0 }}>
                          <label htmlFor="manual-optimize">
                            <Text type="label" display="block">
                              是否优化线路
                            </Text>
                          </label>
                          <Select
                            id="manual-optimize"
                            disabled={busy || !!active}
                            value={optimizeRoutes ? "yes" : "no"}
                            onChange={(e) => {
                              setOptimizeRoutes(e.target.value === "yes");
                              touchEditor();
                            }}
                          >
                            <option value="no">
                              不优化，严格按人工排序核算
                            </option>
                            <option value="yes">
                              优化，搜索约束内最低费用分车与线路
                            </option>
                          </Select>
                        </VStack>
                      </Grid>
                      <Text type="supporting" display="block">
                        {optimizeRoutes
                          ? "指定车辆保持不变；自动专车依据容量与路线分配。小规模枚举，大规模限时搜索，并标明是否已证明最优。"
                          : "指定车辆按人工相对顺序核算；选择自动专车的任务仍会自动分车排线。"}
                      </Text>
                      {dirty && (
                        <Text type="body" as="p">
                          人工选择已修改，费用与地图尚未更新。完成调整后点击“开始核算”。
                        </Text>
                      )}
                      <Input
                        aria-label="搜索调整门店"
                        placeholder="搜索门店编号或名称"
                        value={query}
                        onChange={(e) => {
                          setQuery(e.target.value);
                          setPage(0);
                        }}
                      />
                      <VStack
                        gap={0}
                        width="100%"
                        style={{
                          minWidth: 0,
                          maxWidth: "100%",
                        }}
                      >
                        <Table
                          density="balanced"
                          hasHover
                          dividers="rows"
                          style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                        >
                          <TableHeader>
                            <TableRow>
                              <TableHeaderCell>门店 / 件数</TableHeaderCell>
                              <TableHeaderCell>配送方式与车辆</TableHeaderCell>
                              <TableHeaderCell>当前顺序</TableHeaderCell>
                              <TableHeaderCell>调整顺序</TableHeaderCell>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {visible.map((t) => {
                              const c = choices[t.id] ?? {
                                mode: "unassigned",
                                order: 1,
                              };
                              return (
                                <TableRow key={t.id}>
                                  <TableCell>
                                    <Text
                                      type="label"
                                      weight="semibold"
                                      hasTabularNumbers
                                    >
                                      {t.code} · {t.name}
                                    </Text>
                                    <Text type="supporting" display="block">
                                      {t.pieces} 件 · 共配{" "}
                                      {t.common_unit_price === null
                                        ? "未配置"
                                        : `¥${t.common_unit_price}/件`}
                                    </Text>
                                  </TableCell>
                                  <TableCell>
                                    <Select
                                      aria-label={"配送安排 " + t.code}
                                      value={c.mode}
                                      disabled={busy || !!active}
                                      onChange={(e) =>
                                        change(t.id, e.target.value)
                                      }
                                    >
                                      <option value="dedicated_auto">
                                        专车（自动分车排线）
                                      </option>
                                      <option value="common">共配</option>
                                      <option value="unassigned">未分配</option>
                                      {vehicles.map((v) => (
                                        <option key={v.id} value={v.id}>
                                          {v.plate ?? v.id} · {v.max_pieces}件 ·{" "}
                                          {(v.day_available ?? v.available)
                                            ? "可用"
                                            : "不可用"}
                                        </option>
                                      ))}
                                    </Select>
                                  </TableCell>
                                  <TableCell>
                                    {c.mode === "dedicated_auto"
                                      ? "核算后自动分配"
                                      : ["common", "unassigned"].includes(
                                            c.mode,
                                          )
                                        ? "—"
                                        : taskOrders.get(t.id)}
                                  </TableCell>
                                  <TableCell>
                                    <HStack
                                      gap={3}
                                      wrap="wrap"
                                      align="center"
                                      style={{ minWidth: 0 }}
                                    >
                                      <Action
                                        variant="ghost"
                                        aria-label={"上移 " + t.code}
                                        disabled={
                                          busy ||
                                          !!active ||
                                          [
                                            "common",
                                            "unassigned",
                                            "dedicated_auto",
                                          ].includes(c.mode)
                                        }
                                        onClick={() => move(t.id, -1)}
                                      >
                                        <ArrowUp size={15} />
                                      </Action>
                                      <Action
                                        variant="ghost"
                                        aria-label={"下移 " + t.code}
                                        disabled={
                                          busy ||
                                          !!active ||
                                          [
                                            "common",
                                            "unassigned",
                                            "dedicated_auto",
                                          ].includes(c.mode)
                                        }
                                        onClick={() => move(t.id, 1)}
                                      >
                                        <ArrowDown size={15} />
                                      </Action>
                                    </HStack>
                                  </TableCell>
                                </TableRow>
                              );
                            })}
                          </TableBody>
                        </Table>
                      </VStack>
                      <Pagination
                        label="人工调整"
                        total={filtered.length}
                        page={editorPage}
                        pageSize={pageSize}
                        disabled={busy || !!active}
                        onPageChange={setPage}
                        onPageSizeChange={(size) => {
                          setPageSize(size);
                          setPage(0);
                        }}
                      />
                      <label htmlFor="plan-note">
                        <Text type="label" display="block">
                          版本备注
                        </Text>
                      </label>
                      <Input
                        id="plan-note"
                        maxLength={240}
                        placeholder="填写本次调整原因（选填）"
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                      />
                    </VStack>
                  </Section>
                )}
                <HStack as="nav" gap={2} wrap="wrap" aria-label="方案详情">
                  {(
                    [
                      ["routes", "专车线路"],
                      ["common", `共配 ${result.common.length} 票`],
                      ["errors", `异常 ${result.unassigned.length} 票`],
                      ["map", "地图与行驶轨迹"],
                    ] as const
                  ).map(([id, label]) => (
                    <Action
                      key={id}
                      onClick={() => {
                        setSub(id);
                        setResultPage(0);
                      }}
                      variant={sub === id ? "primary" : "secondary"}
                    >
                      {label}
                    </Action>
                  ))}
                </HStack>
                {sub === "routes" && (
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    {result.routes.map((r, i) => (
                      <VStack
                        as="article"
                        gap={4}
                        paddingBlock={4}
                        key={r.vehicle_id + "-" + i}
                      >
                        <HStack as="header" gap={3} wrap="wrap" align="center">
                          <HStack
                            width="var(--spacing-2)"
                            height="var(--spacing-2)"
                            aria-hidden="true"
                            style={{ background: colors[i % colors.length] }}
                          />
                          <Heading level={3}>{r.plate ?? r.vehicle_id}</Heading>
                          <Text type="body">
                            {r.pieces}/
                            {vehicles.find((v) => v.id === r.vehicle_id)
                              ?.max_pieces ?? "—"}{" "}
                            件 · {(r.meters / 1000).toFixed(3)} km
                          </Text>
                          <Text
                            type="label"
                            weight="semibold"
                            hasTabularNumbers
                          >
                            ¥ {r.fee ?? "待核验"}
                          </Text>
                        </HStack>
                        <Text type="body" as="p">
                          {r.task_ids
                            .map((id) => taskById.get(id)?.name ?? id)
                            .join(" → ")}
                          {Number(r.regional_surcharge) > 0 && (
                            <Text type="supporting" display="block">
                              报价已含区域附加费 ¥{r.regional_surcharge}
                              ，按当前报价规则收取。
                            </Text>
                          )}
                        </Text>
                        <HStack
                          gap={3}
                          wrap="wrap"
                          align="center"
                          style={{ minWidth: 0 }}
                        >
                          <Text type="body">
                            道路轨迹 {roadSegments(r).length}/
                            {r.task_ids.length} 段
                          </Text>
                          <Action
                            variant="secondary"
                            aria-label={
                              "查看行驶轨迹 " + (r.plate ?? r.vehicle_id)
                            }
                            disabled={!!example}
                            onClick={() =>
                              setRouteMapId(
                                routeMapId === r.vehicle_id
                                  ? null
                                  : r.vehicle_id,
                              )
                            }
                          >
                            {routeMapId === r.vehicle_id
                              ? "收起轨迹"
                              : "查看行驶轨迹"}
                          </Action>
                        </HStack>
                        {routeMapId === r.vehicle_id && inlineMap && (
                          <VStack
                            gap={3}
                            style={{ minWidth: 0, width: "100%" }}
                          >
                            <Text type="body" as="p">
                              仓库出发，按配送顺序展示；箭头表示行驶方向。
                            </Text>
                            <LocationMap
                              points={inlineMap.points}
                              lines={inlineMap.lines}
                              label={(r.plate ?? r.vehicle_id) + "行驶轨迹"}
                            />
                            {inlineMap.lines.length < r.task_ids.length && (
                              <Text type="body" as="p">
                                {inlineMap.lines.length
                                  ? `已返回 ${inlineMap.lines.length}/${r.task_ids.length} 段道路，缺失路段未绘制。`
                                  : "该方案尚无真实道路轨迹，请关联仓库和门店坐标后，使用高德算路重新规划或重算。"}
                              </Text>
                            )}
                          </VStack>
                        )}
                        {r.errors.map((e, n) => (
                          <ErrorMessage key={n} text={e.message} />
                        ))}
                        <VStack
                          gap={0}
                          width="100%"
                          style={{
                            minWidth: 0,
                            maxWidth: "100%",
                          }}
                        >
                          <Table
                            density="balanced"
                            hasHover
                            dividers="rows"
                            style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                          >
                            <TableHeader>
                              <TableRow>
                                <TableHeaderCell>顺序 / 门店</TableHeaderCell>
                                <TableHeaderCell>件数</TableHeaderCell>
                                <TableHeaderCell>预计到店</TableHeaderCell>
                                <TableHeaderCell>
                                  开始卸货 / 等待
                                </TableHeaderCell>
                                <TableHeaderCell>预计离店</TableHeaderCell>
                              </TableRow>
                            </TableHeader>
                            <TableBody>
                              {r.stops.map((s, n) => (
                                <TableRow key={s.task_id + "-" + n}>
                                  <TableCell>
                                    {n + 1} ·{" "}
                                    {taskById.get(s.task_id)?.name ?? s.task_id}
                                  </TableCell>
                                  <TableCell>{s.pieces}</TableCell>
                                  <TableCell>
                                    {shortTime(s.arrival_at)}
                                  </TableCell>
                                  <TableCell>
                                    {shortTime(s.unload_start_at)}
                                    <Text type="supporting" display="block">
                                      等待{" "}
                                      {Number((s.wait_seconds / 60).toFixed(1))}{" "}
                                      分钟
                                    </Text>
                                  </TableCell>
                                  <TableCell>
                                    {shortTime(s.departure_at)}
                                  </TableCell>
                                </TableRow>
                              ))}
                            </TableBody>
                          </Table>
                        </VStack>
                      </VStack>
                    ))}
                    {!result.routes.length && (
                      <Text type="body" as="p">
                        此版本没有专车线路。请在共配与异常中核对全部任务。
                      </Text>
                    )}
                  </VStack>
                )}
                {sub === "common" && (
                  <>
                    <Text type="supporting" as="p">
                      共配仅核算件数和单价，不安排或校验到店时间、营业时间、卸货时间及最晚到店限制。
                    </Text>
                    <HStack
                      gap={3}
                      wrap="wrap"
                      align="center"
                      style={{ minWidth: 0 }}
                    >
                      <Input
                        aria-label="搜索共配门店"
                        placeholder="搜索共配门店编号或名称"
                        value={resultQuery}
                        onChange={(e) => {
                          setResultQuery(e.target.value);
                          setResultPage(0);
                        }}
                      />
                      <Text type="body">{filteredCommon.length} 票</Text>
                    </HStack>
                    <VStack
                      gap={0}
                      width="100%"
                      style={{
                        minWidth: 0,
                        maxWidth: "100%",
                      }}
                    >
                      <Table
                        density="balanced"
                        hasHover
                        dividers="rows"
                        style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                      >
                        <TableHeader>
                          <TableRow>
                            <TableHeaderCell>门店</TableHeaderCell>
                            <TableHeaderCell>件数</TableHeaderCell>
                            <TableHeaderCell>费用</TableHeaderCell>
                            <TableHeaderCell>到店时间</TableHeaderCell>
                            <TableHeaderCell>原因</TableHeaderCell>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {filteredCommon
                            .slice(
                              commonPage * resultSize,
                              commonPage * resultSize + resultSize,
                            )
                            .map((r) => (
                              <TableRow key={r.task_id}>
                                <TableCell>
                                  {taskById.get(r.task_id)?.name ?? r.task_id}
                                </TableCell>
                                <TableCell>{r.pieces}</TableCell>
                                <TableCell>¥ {r.fee ?? "未配置"}</TableCell>
                                <TableCell>无需校验</TableCell>
                                <TableCell>
                                  {/到店|经营|卸货|时间/.test(r.reason) &&
                                  !/共配.*不校验/.test(r.reason)
                                    ? `历史专车候选说明（共配不校验到店）：${r.reason}`
                                    : r.reason}
                                </TableCell>
                              </TableRow>
                            ))}
                        </TableBody>
                      </Table>
                      {!filteredCommon.length && (
                        <Text type="body" as="p">
                          没有匹配的共配任务。
                        </Text>
                      )}
                    </VStack>
                    <Pagination
                      label="共配任务"
                      total={filteredCommon.length}
                      page={commonPage}
                      pageSize={resultSize}
                      onPageChange={setResultPage}
                      onPageSizeChange={(size) => {
                        setResultSize(size);
                        setResultPage(0);
                      }}
                    />
                  </>
                )}
                {sub === "errors" && (
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    {proof.issues
                      .slice(
                        conflictPage * issueSize,
                        conflictPage * issueSize + issueSize,
                      )
                      .map((e, i) => (
                        <ErrorMessage
                          key={i}
                          text={`${e.task_id ? (taskById.get(e.task_id)?.name ?? e.task_id) : ""} ${e.message}`}
                        />
                      ))}
                    {proof.issues.length > 0 && (
                      <Pagination
                        label="核算冲突"
                        total={proof.issues.length}
                        page={conflictPage}
                        pageSize={issueSize}
                        onPageChange={setIssuePage}
                        onPageSizeChange={(size) => {
                          setIssueSize(size);
                          setIssuePage(0);
                        }}
                      />
                    )}
                    {result.unassigned
                      .slice(
                        unassignedPage * resultSize,
                        unassignedPage * resultSize + resultSize,
                      )
                      .map((r) => (
                        <VStack
                          as="article"
                          gap={4}
                          paddingBlock={4}
                          key={r.task_id}
                        >
                          <Text
                            type="label"
                            weight="semibold"
                            hasTabularNumbers
                          >
                            {taskById.get(r.task_id)?.name ?? r.task_id} ·{" "}
                            {r.pieces} 件
                          </Text>
                          <Text type="body" as="p">
                            {r.reason}
                          </Text>
                        </VStack>
                      ))}
                    {result.unassigned.length > 0 && (
                      <Pagination
                        label="未分配任务"
                        total={result.unassigned.length}
                        page={unassignedPage}
                        pageSize={resultSize}
                        onPageChange={setResultPage}
                        onPageSizeChange={(size) => {
                          setResultSize(size);
                          setResultPage(0);
                        }}
                      />
                    )}
                    {proof.valid && (
                      <Text type="body" as="p">
                        <HStack as="span" gap={2} vAlign="center">
                          <CheckCircle2 size={17} />{" "}
                          全部任务已分配，分配校验通过。
                        </HStack>
                      </Text>
                    )}
                  </VStack>
                )}
                {sub === "map" && (
                  <VStack gap={3} style={{ minWidth: 0 }}>
                    <Text type="body" as="p">
                      按方案配送顺序展示高德道路轨迹，颜色与专车线路一致，箭头表示方向；共配显示门店位置。
                    </Text>
                    <label htmlFor="map-vehicle">
                      <Text type="label" display="block">
                        查看车辆轨迹
                      </Text>
                    </label>
                    <Select
                      id="map-vehicle"
                      value={mapVehicle}
                      onChange={(e) => setMapVehicle(e.target.value)}
                    >
                      <option value="all">全部车辆</option>
                      {result.routes.map((r) => (
                        <option key={r.vehicle_id} value={r.vehicle_id}>
                          {r.plate ?? r.vehicle_id} · {roadSegments(r).length}/
                          {r.task_ids.length}段
                        </option>
                      ))}
                    </Select>
                    {example ? (
                      <VStack
                        gap={3}
                        paddingBlock={6}
                        align="center"
                        style={{ minWidth: 0, textAlign: "center" }}
                      >
                        此固定样例没有真实坐标与道路折线，仅用于费用和时序回归。
                      </VStack>
                    ) : (
                      <LocationMap
                        points={mapped.points}
                        lines={mapped.lines}
                        label="方案配送地图"
                      />
                    )}
                    {!mapped.lines.length && !example && (
                      <Text type="supporting" as="p">
                        当前方案没有返回道路轨迹；请使用高德算路重新规划或重算，缺失路段不会以直线代替。
                      </Text>
                    )}
                  </VStack>
                )}
                {!example && plan && (
                  <HStack
                    gap={3}
                    wrap="wrap"
                    align="center"
                    justify="between"
                    style={{ minWidth: 0 }}
                  >
                    <Text type="body">
                      {plan.status === "CONFIRMED"
                        ? "此版本已人工确认"
                        : !result.confirmable
                          ? result.road_source === "DRIVING"
                            ? "驾车参考方案需人工核对货车限行，当前不能确认执行"
                            : "请先处理异常或完成专车道路核验"
                          : planStale
                            ? "输入已变化，需要重新规划"
                            : "全部任务已复核，可人工确认"}
                    </Text>
                    <Link
                      isStandalone
                      href={"/api/planning/plans/" + plan.id + "/export.json"}
                    >
                      下载证据 JSON
                    </Link>
                    <Action
                      variant="primary"
                      disabled={
                        busy ||
                        !!active ||
                        dirty ||
                        planStale ||
                        !result.confirmable ||
                        plan.version !== head ||
                        plan.status === "CONFIRMED"
                      }
                      onClick={() => void versionAction("confirm")}
                    >
                      <CheckCircle2 size={15} /> 确认此方案
                    </Action>
                  </HStack>
                )}
              </>
            )}
          </VStack>
        </Section>
        <VStack
          as="aside"
          gap={4}
          style={{
            flex: "1 1 0",
            minWidth: "min(100%, calc(var(--spacing-10) * 7))",
          }}
        >
          <Section
            padding={4}
            style={{
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              gap: "var(--spacing-4)",
            }}
          >
            <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
              <HStack
                gap={3}
                wrap="wrap"
                align="center"
                justify="between"
                style={{ minWidth: 0 }}
              >
                <HStack
                  gap={3}
                  wrap="wrap"
                  align="center"
                  style={{ minWidth: 0 }}
                >
                  <History size={16} />
                  <Heading level={2}>历史版本</Heading>
                </HStack>
                <Action
                  variant="ghost"
                  aria-label="刷新方案历史"
                  onClick={() => {
                    loadVersions().catch((e) => setError(text(e)));
                  }}
                >
                  <RefreshCw size={14} />
                </Action>
              </HStack>
              {versions.map((v) => (
                <Action
                  style={{
                    width: "100%",
                    height: "auto",
                    whiteSpace: "normal",
                    textAlign: "start",
                    flexDirection: "column",
                    alignItems: "stretch",
                    gap: "var(--spacing-2)",
                    padding: "var(--spacing-3)",
                    background:
                      !example && plan?.id === v.id
                        ? "var(--color-accent-muted)"
                        : undefined,
                  }}
                  key={v.id}
                  disabled={busy || !!active}
                  onClick={() =>
                    void openPlan(v.id).catch((e) => setError(text(e)))
                  }
                  variant="secondary"
                >
                  <VStack as="span" gap={2} width="100%" style={{minWidth:0}}>
                  <HStack as="span" gap={2} hAlign="between" wrap="wrap">
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      V{v.version} ·{" "}
                      {v.status === "CONFIRMED" ? "已确认" : "草稿"}
                    </Text>
                    <Text type="large" weight="semibold" hasTabularNumbers>
                      ¥{v.total_fee}
                    </Text>
                  </HStack>
                  <Text type="supporting" display="block">
                    {shortTime(v.created_at)}
                  </Text>
                  <Text type="supporting" display="block">
                    {v.created_by} ·{" "}
                    {v.valid
                      ? "校验通过"
                      : v.blocking_issue_count === 0
                        ? "分配校验通过，驾车参考待核验"
                        : "有冲突"}
                    {v.input_changed ? " · 输入已变更" : ""}
                  </Text>
                  </VStack>
                </Action>
              ))}
              {!versions.length && (
                <Text type="body" as="p">
                  尚未保存方案
                </Text>
              )}
              <Pagination
                label="方案历史"
                total={versionTotal}
                page={Math.floor(offset / historySize)}
                pageSize={historySize}
                disabled={busy || !!active}
                onPageChange={(n) => {
                  const start = n * historySize;
                  setOffset(start);
                  void loadVersions(start).catch((e) => setError(text(e)));
                }}
                onPageSizeChange={(size) => {
                  setHistorySize(size);
                  setOffset(0);
                  void loadVersions(0, false, size).catch((e) =>
                    setError(text(e)),
                  );
                }}
              />
              {plan && !example && plan.version !== head && (
                <Action
                  variant="secondary"
                  disabled={busy || !!active}
                  onClick={() => void versionAction("restore")}
                >
                  <Save size={14} /> 从此版本恢复新草稿
                </Action>
              )}
            </VStack>
          </Section>
          <Section
            padding={4}
            style={{
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              gap: "var(--spacing-4)",
            }}
          >
            <VStack gap={4} width="100%" style={{ minWidth: 0 }}>
              <Heading level={3}>固定道路演练</Heading>
              <Text type="body" as="p">
                检查联合排线、改车、改方式及顺序，不产生真实接口费用。
              </Text>
              <Action
                variant="secondary"
                disabled={busy || !!active}
                onClick={() => void showExample("MIXED_56")}
              >
                混合配送 · 预期 ¥56
              </Action>
              <Action
                variant="secondary"
                disabled={busy || !!active}
                onClick={() => void showExample("SHARED_40")}
              >
                车辆共享 · 预期 ¥40
              </Action>
            </VStack>
          </Section>
        </VStack>
      </HStack>
    </VStack>
  );
}
