import { Collapsible } from "@astryxdesign/core/Collapsible";
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
import React, { FormEvent, useEffect, useState } from "react";
import {
  Archive,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  History,
  RefreshCw,
  Upload,
} from "lucide-react";
import { api, ApiError } from "./api";
import { Row, QuoteVersion, quoteDescription, Catalog } from "./master-data";
import { ErrorMessage, Modal, Spinner } from "./ui";
import { Pagination, PageSize, pageIndex } from "./pagination";
type Source = {
  row: number;
  pieces: number;
  supplied_name: string;
  supplied_code: string;
};
type WarehouseTotal = {
  warehouse_id: string;
  warehouse_code: string;
  warehouse_name: string;
  task_count: number;
  total_pieces: number;
};
type Totals = {
  task_count: number;
  total_pieces: number;
  active_warehouse_count: number;
  warehouses: WarehouseTotal[];
};
export type DemandTask = {
  id: string;
  store_id: string;
  store_code: string;
  store_name: string;
  warehouse_id: string;
  warehouse_name: string;
  import_warehouse_id?: string;
  pieces: number;
  source_rows: Source[];
  configuration_warnings: string[];
  latest_arrival_at: string | null;
  latest_arrival_source?: "DAY" | "LEGACY" | "BUSINESS_CLOSE";
  business_close_at?: string;
  day_revision: number;
};
type SnapshotMeta = {
  id: string;
  delivery_date: string;
  version: number;
  demand_revision: number;
  content_hash: string;
  created_at: string;
  created_by: string;
  totals: Totals;
  input_changed: boolean;
  changed_reasons: string[];
};
export type DayData = {
  delivery_date: string;
  revision: number;
  input_fingerprint: string;
  latest_import_id: string | null;
  items: DemandTask[];
  totals: Totals;
  latest_snapshot: SnapshotMeta | null;
  snapshot_count: number;
};
type ImportResult = {
  can_commit: boolean;
  source_row_count: number;
  blank_rows: number;
  error_count: number;
  errors: {
    row: number;
    field: string;
    message: string;
  }[];
  zero_row_count: number;
  zero_rows: Source[];
  merged_row_count: number;
  selected_date_row_count?: number;
  other_date_row_count?: number;
  ignored_nonbusiness_row_count?: number;
  before: Totals;
  after: Totals;
  changes: {
    added: number;
    removed: number;
    updated: number;
  };
  configuration_warning_count: number;
  items: DemandTask[];
};
type Mapping = {
  store_name: string;
  pieces: string;
  store_code: string;
  delivery_date: string;
};
type SourceInspection = {
  sheets: {
    name: string;
    sample_rows: {
      row: number;
      values: (string | null)[];
    }[];
  }[];
  suggested: {
    sheet_name: string;
    header_row: number;
  };
};
type Uploaded = {
  id: string;
  delivery_date: string;
  preview_revision: number;
  columns: {
    index: number;
    label: string;
  }[];
  source_row_count: number;
  source?: {
    sheet_name: string;
    header_row: number;
    ignored_sheets: string[];
  };
  sample_rows: {
    row: number;
    values: unknown[];
  }[];
  suggested_mapping: Record<keyof Mapping, number | null>;
  result: ImportResult | null;
};
type SnapshotDetail = SnapshotMeta & {
  input: {
    delivery_date: string;
    demand_revision: number;
    tasks: DemandTask[];
    stores: (Row & {
      business_open_at: string;
      business_close_at: string;
      latest_arrival_at: string | null;
    })[];
    warehouses: Row[];
    vehicles: Row[];
    quotes: QuoteVersion[];
    road_status: string;
  };
};
const errorText = (e: unknown) =>
  e instanceof Error ? e.message : "操作未完成，请重试。";
const clock = (s: string | null | undefined) =>
  s ? s.replace("T", " ").slice(0, 16) : "未配置";
const number = (n: number) => n.toLocaleString("zh-CN");
function columnLabel(index: number) {
  let text = "",
    n = index + 1;
  while (n) {
    n--;
    text = String.fromCharCode(65 + (n % 26)) + text;
    n = Math.floor(n / 26);
  }
  return text;
}
export function DemandPanel({
  day,
  groupId,
  catalog,
  data,
  loading,
  onReload,
  notify,
}: {
  groupId?: string;
  catalog?: Catalog;
  day: string;
  data: DayData | null;
  loading: boolean;
  onReload: () => void;
  notify: (s: string) => void;
}) {
  const [manual, setManual] = useState<{
    mode: "new" | "edit" | "delete";
    task?: DemandTask;
  } | null>(null);
  const scope = groupId ? "?group_id=" + encodeURIComponent(groupId) : "";
  const [importing, setImporting] = useState(false);
  const [snapshotId, setSnapshotId] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const [deadline, setDeadline] = useState<DemandTask | null>(null);
  const [sources, setSources] = useState<{
    name: string;
    rows: Source[];
  } | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  const [query, setQuery] = useState("");
  const [freezing, setFreezing] = useState(false);
  const [error, setError] = useState("");
  const current = data?.delivery_date === day ? data : null;
  const latest = current?.latest_snapshot;
  const rows = (current?.items ?? []).filter((t) =>
    (t.store_code + t.store_name + t.warehouse_name).includes(query),
  );
  const pages = Math.ceil(rows.length / pageSize);
  const shown = Math.min(page, Math.max(0, pages - 1));
  async function capture() {
    if (!current) return;
    setFreezing(true);
    setError("");
    try {
      const snapshot = await api<SnapshotMeta>(
        "/planning/days/" + day + "/snapshots" + scope,
        "POST",
        {
          expected_revision: current.revision,
          expected_fingerprint: current.input_fingerprint,
        },
      );
      notify("已冻结规划输入 V" + snapshot.version);
      onReload();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) onReload();
    } finally {
      setFreezing(false);
    }
  }
  return (
    <>
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
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <FileSpreadsheet size={18} />
              <Heading level={2}>每日配送需求</Heading>
            </HStack>
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Link
                isStandalone
                href="/api/planning/demand-template.xlsx"
                download
              >
                <HStack as="span" gap={2} vAlign="center">
                  <Download size={14} /> 下载需求模板
                </HStack>
              </Link>
              <Action
                variant="primary"
                disabled={loading}
                onClick={() => setImporting(true)}
              >
                <Upload size={14} /> 导入当日需求
              </Action>
              {groupId && catalog && (
                <Action
                  variant="primary"
                  disabled={loading}
                  onClick={() => setManual({ mode: "new" })}
                >
                  新建需求
                </Action>
              )}
            </HStack>
          </HStack>
          <Grid
            columns={{ minWidth: 160, max: 4 }}
            gap={4}
            style={{ minWidth: 0 }}
          >
            <VStack gap={3} style={{ minWidth: 0 }}>
              <Text type="body">配送日期</Text>
              <Text type="label" weight="semibold" hasTabularNumbers>
                {day}
              </Text>
            </VStack>
            <VStack gap={3} style={{ minWidth: 0 }}>
              <Text type="body">配送门店 / 任务</Text>
              <Text type="label" weight="semibold" hasTabularNumbers>
                {number(current?.totals.task_count ?? 0)}
                <Text type="supporting"> 票</Text>
              </Text>
            </VStack>
            <VStack gap={3} style={{ minWidth: 0 }}>
              <Text type="body">总件数</Text>
              <Text type="label" weight="semibold" hasTabularNumbers>
                {number(current?.totals.total_pieces ?? 0)}
                <Text type="supporting"> 件</Text>
              </Text>
            </VStack>
            <VStack gap={3} style={{ minWidth: 0 }}>
              <Text type="body">需求修订</Text>
              <Text type="label" weight="semibold" hasTabularNumbers>
                {current?.revision ? "R" + current.revision : "未导入"}
              </Text>
            </VStack>
          </Grid>
          <Text type="supporting" as="p">
            一次导入替换当前分组内全部仓库的需求；页面上方的仓库筛选不会缩小替换范围。同店重复行汇总为一票，零件数跳过。
          </Text>
          {current && current.revision > 0 && (
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              {current.totals.warehouses.map((w) => (
                <VStack gap={3} style={{ minWidth: 0 }} key={w.warehouse_id}>
                  <Text type="label" weight="semibold" hasTabularNumbers>
                    {w.warehouse_name}
                  </Text>
                  <Text type="body">
                    {w.task_count} 票 · {number(w.total_pieces)} 件
                  </Text>
                </VStack>
              ))}
            </HStack>
          )}
          <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
            <Heading level={3}>当日需求明细</Heading>
            <Input
              aria-label="搜索当日需求"
              placeholder="搜索门店、编号或仓库"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(0);
              }}
            />
          </HStack>
          <VStack
            gap={0}
            width="100%"
            style={{ minWidth: 0, maxWidth: "100%" }}
          >
            <Table
              density="balanced"
              hasHover
              dividers="rows"
              style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
            >
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>门店 / 出货仓</TableHeaderCell>
                  <TableHeaderCell>件数</TableHeaderCell>
                  <TableHeaderCell>来源</TableHeaderCell>
                  <TableHeaderCell>当天最晚到店</TableHeaderCell>
                  <TableHeaderCell>资料提示</TableHeaderCell>
                  <TableHeaderCell>操作</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows
                  .slice(shown * pageSize, shown * pageSize + pageSize)
                  .map((task) => (
                    <TableRow key={task.store_id}>
                      <TableCell>
                        <Text type="label" weight="semibold" hasTabularNumbers>
                          {task.store_name}
                        </Text>
                        <Text type="supporting" display="block">
                          {task.store_code} · {task.warehouse_name}
                        </Text>
                        {task.import_warehouse_id !== task.warehouse_id && (
                          <Text type="supporting" display="block">
                            归仓资料已更新
                          </Text>
                        )}
                      </TableCell>
                      <TableCell>
                        <Text type="large" weight="semibold" hasTabularNumbers>
                          {number(task.pieces)}
                        </Text>
                      </TableCell>
                      <TableCell>
                        <Action
                          variant="ghost"
                          onClick={() =>
                            setSources({
                              name: task.store_name,
                              rows: task.source_rows,
                            })
                          }
                        >
                          {task.source_rows.length} 行来源
                        </Action>
                      </TableCell>
                      <TableCell>
                        {clock(task.latest_arrival_at)}
                        {task.latest_arrival_source === "BUSINESS_CLOSE" && (
                          <Text type="supporting" display="block">
                            随营业结束
                          </Text>
                        )}
                      </TableCell>
                      <TableCell>
                        {task.configuration_warnings.length
                          ? task.configuration_warnings.join("；")
                          : "基础配置齐全"}
                      </TableCell>
                      <TableCell>
                        <Action
                          variant="ghost"
                          disabled={loading}
                          onClick={() => setDeadline(task)}
                        >
                          收货限制
                        </Action>
                        {groupId && (
                          <>
                            <Action
                              variant="ghost"
                              onClick={() => setManual({ mode: "edit", task })}
                            >
                              修改件数
                            </Action>
                            <Action
                              variant="ghost"
                              onClick={() =>
                                setManual({ mode: "delete", task })
                              }
                            >
                              移除需求
                            </Action>
                          </>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </VStack>
          {!rows.length && !loading && (
            <VStack
              gap={3}
              paddingBlock={6}
              align="center"
              style={{ minWidth: 0, textAlign: "center" }}
            >
              <FileSpreadsheet size={28} />
              <Heading level={3}>
                {query
                  ? "没有匹配的需求"
                  : current?.revision
                    ? "当日没有配送任务"
                    : "还未导入当日配送需求"}
              </Heading>
              <Text type="body" as="p">
                {query
                  ? "请更换搜索关键词。"
                  : "下载模板，填写门店名称和件数后上传预览。"}
              </Text>
            </VStack>
          )}
          <Pagination
            label="当日需求"
            page={shown}
            total={rows.length}
            pageSize={pageSize}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(0);
            }}
          />
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
          <HStack
            gap={3}
            wrap="wrap"
            align="center"
            justify="between"
            style={{ minWidth: 0 }}
          >
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Archive size={18} />
              <Heading level={2}>规划输入快照</Heading>
            </HStack>
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Action
                variant="secondary"
                disabled={!current?.snapshot_count}
                onClick={() => setHistory(true)}
              >
                <History size={14} /> 历史快照 {current?.snapshot_count ?? 0}
              </Action>
              <Action
                variant="primary"
                disabled={
                  loading ||
                  freezing ||
                  !current?.revision ||
                  Boolean(latest && !latest.input_changed)
                }
                onClick={() => void capture()}
              >
                {freezing ? <Spinner /> : <Archive size={14} />}{" "}
                {latest ? "重新冻结输入" : "冻结当前输入"}
              </Action>
            </HStack>
          </HStack>
          <ErrorMessage text={error} />
          {latest ? (
            <VStack gap={3} style={{ minWidth: 0 }}>
              <VStack gap={3} style={{ minWidth: 0 }}>
                <Text type="body" color="accent">
                  输入 V{latest.version}
                </Text>
                <Text type="body">
                  {latest.input_changed
                    ? "输入已变更，需重新冻结"
                    : "与当前输入一致"}
                </Text>
                <Heading level={3}>
                  {number(latest.totals.task_count)} 票 ·{" "}
                  {number(latest.totals.total_pieces)} 件
                </Heading>
                <Text type="body" as="p">
                  {clock(latest.created_at)} · {latest.created_by} · 需求 R
                  {latest.demand_revision}
                </Text>
                {latest.input_changed && (
                  <Text type="supporting" as="p">
                    {latest.changed_reasons.join("；")}
                  </Text>
                )}
                <Text
                  type="code"
                  display="block"
                  style={{ overflowWrap: "anywhere" }}
                >
                  {latest.content_hash}
                </Text>
              </VStack>
              <Action
                variant="secondary"
                onClick={() => setSnapshotId(latest.id)}
              >
                查看冻结内容
              </Action>
            </VStack>
          ) : (
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
                确认当日需求时自动生成快照。之后调整报价、归仓、发车或收货限制，可重新冻结并保留旧版。
              </VStack>
            </Section>
          )}
          <Text type="supporting" as="p">
            快照保存需求、来源行、仓库、门店、车辆、有效报价和北京时间限制。冻结输入不代表已经生成或验证物流线路。
          </Text>
        </VStack>
      </Section>
      {importing && (
        <DemandImportDialog
          groupId={groupId}
          day={day}
          onClose={() => setImporting(false)}
          onDone={(message) => {
            setImporting(false);
            setPage(0);
            notify(message);
            onReload();
          }}
        />
      )}
      {manual && groupId && catalog && (
        <ManualDemandDialog
          groupId={groupId}
          current={current}
          catalog={catalog}
          mode={manual.mode}
          task={manual.task}
          onClose={() => setManual(null)}
          onSave={() => {
            setManual(null);
            notify("分组需求已保存，已生成最新快照");
            onReload();
          }}
        />
      )}
      {snapshotId && (
        <SnapshotViewer id={snapshotId} onClose={() => setSnapshotId(null)} />
      )}
      {history && (
        <SnapshotHistory
          groupId={groupId}
          day={day}
          onClose={() => setHistory(false)}
        />
      )}
      {sources && (
        <SourceRows
          name={sources.name}
          rows={sources.rows}
          onClose={() => setSources(null)}
        />
      )}
      {deadline && (
        <DeadlineEditor
          groupId={groupId}
          task={deadline}
          day={day}
          onClose={() => setDeadline(null)}
          onSave={() => {
            setDeadline(null);
            notify("当日收货限制已保存，请重新冻结输入");
            onReload();
          }}
        />
      )}
    </>
  );
}
function SourceRows({
  name,
  rows,
  onClose,
}: {
  name: string;
  rows: Source[];
  onClose: () => void;
}) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  return (
    <Modal
      wide
      title="来源行明细"
      description={name + " · " + rows.length + " 行"}
      onClose={onClose}
    >
      <VStack gap={0} width="100%" style={{ minWidth: 0, maxWidth: "100%" }}>
        <Table
          density="balanced"
          hasHover
          dividers="rows"
          style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
        >
          <TableHeader>
            <TableRow>
              <TableHeaderCell>原文件行</TableHeaderCell>
              <TableHeaderCell>门店名称</TableHeaderCell>
              <TableHeaderCell>编号</TableHeaderCell>
              <TableHeaderCell>件数</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows
              .slice(page * pageSize, page * pageSize + pageSize)
              .map((s) => (
                <TableRow key={s.row}>
                  <TableCell>{s.row}</TableCell>
                  <TableCell>{s.supplied_name || "—"}</TableCell>
                  <TableCell>{s.supplied_code || "—"}</TableCell>
                  <TableCell>{s.pieces}</TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </VStack>
      <Pagination
        label="来源行"
        page={page}
        total={rows.length}
        pageSize={pageSize}
        onPageChange={setPage}
        onPageSizeChange={(size) => {
          setPageSize(size);
          setPage(0);
        }}
      />
    </Modal>
  );
}
function DemandImportDialog({
  day,
  groupId,
  onClose,
  onDone,
}: {
  groupId?: string;
  day: string;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [uploaded, setUploaded] = useState<Uploaded | null>(null);
  const [mapping, setMapping] = useState<Mapping>({
    store_name: "",
    pieces: "",
    store_code: "",
    delivery_date: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [sources, setSources] = useState<{
    name: string;
    rows: Source[];
  } | null>(null);
  const [inspection, setInspection] = useState<SourceInspection | null>(null);
  const [sheetName, setSheetName] = useState("");
  const [headerRow, setHeaderRow] = useState(1);
  const result = uploaded?.result;
  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    setUploaded(null);
    setConfirmed(false);
    try {
      if (file.size > 5 * 1024 * 1024)
        throw new Error("需求文件不能超过5MiB。");
      if (!inspection) {
        const info = await api<SourceInspection>(
          "/planning/demand-imports/inspect?filename=" +
            encodeURIComponent(file.name),
          "POST",
          file,
        );
        setInspection(info);
        setSheetName(info.suggested.sheet_name);
        setHeaderRow(info.suggested.header_row);
        return;
      }
      const data = await api<Uploaded>(
        "/planning/demand-imports/upload?delivery_date=" +
          day +
          "&filename=" +
          encodeURIComponent(file.name) +
          "&sheet_name=" +
          encodeURIComponent(sheetName) +
          "&header_row=" +
          headerRow +
          (groupId ? "&group_id=" + encodeURIComponent(groupId) : ""),
        "POST",
        file,
      );
      setUploaded(data);
      setMapping({
        ...{ store_name: "", pieces: "", store_code: "", delivery_date: "" },
        ...Object.fromEntries(
          Object.entries(data.suggested_mapping).map(([k, v]) => [
            k,
            v === null ? "" : String(v),
          ]),
        ),
      } as Mapping);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function preview() {
    if (!uploaded) return;
    setBusy(true);
    setError("");
    setConfirmed(false);
    setUploaded((v) => (v ? { ...v, result: null } : v));
    try {
      const response = await api<Uploaded>(
        "/planning/demand-imports/" + uploaded.id + "/preview",
        "POST",
        {
          store_name: Number(mapping.store_name),
          pieces: Number(mapping.pieces),
          store_code:
            mapping.store_code === "" ? null : Number(mapping.store_code),
          delivery_date:
            mapping.delivery_date === "" ? null : Number(mapping.delivery_date),
        },
      );
      setUploaded(response);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function commit() {
    if (!uploaded || !result?.can_commit || !confirmed) return;
    setBusy(true);
    setError("");
    try {
      const receipt = await api<{
        revision: number;
        totals: Totals;
        snapshot_version: number;
      }>("/planning/demand-imports/" + uploaded.id + "/commit", "POST", {
        delivery_date: day,
        preview_revision: uploaded.preview_revision,
      });
      onDone(
        `已替换 ${day} 需求：${receipt.totals.task_count}票、${receipt.totals.total_pieces}件，已保存输入V${receipt.snapshot_version}`,
      );
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) {
        setUploaded((v) => (v ? { ...v, result: null } : v));
        setConfirmed(false);
      }
    } finally {
      setBusy(false);
    }
  }
  function changeMapping(key: keyof Mapping, value: string) {
    setMapping((v) => ({ ...v, [key]: value }));
    setUploaded((v) => (v ? { ...v, result: null } : v));
    setConfirmed(false);
  }
  return (
    <>
      <Modal
        wide
        title="导入每日配送需求"
        description={`${day} · 将替换当前分组全部仓库的需求，当前仓库筛选不影响导入范围。`}
        onClose={onClose}
        busy={busy}
      >
        <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
          <Text type="label" weight="semibold" color="accent">
            1 上传文件
          </Text>
          <Text type="body">→</Text>
          <Text type="label" weight="semibold" hasTabularNumbers>
            2 列匹配与校验
          </Text>
          <Text type="body">→</Text>
          <Text type="label" weight="semibold" hasTabularNumbers>
            3 确认替换并冻结
          </Text>
        </HStack>
        <VStack as="form" gap={4} onSubmit={upload}>
          <label htmlFor="demand-xlsx">
            <Text type="label" display="block">
              选择每日需求文件
            </Text>
          </label>
          <Input
            id="demand-xlsx"
            type="file"
            accept=".xlsx"
            required
            disabled={busy}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setInspection(null);
              setUploaded(null);
              setError("");
              setConfirmed(false);
            }}
          />
          {inspection && (
            <Grid
              columns={{ minWidth: 220, max: 3 }}
              gap={4}
              style={{ minWidth: 0 }}
            >
              <VStack gap={3} style={{ minWidth: 0 }}>
                <label htmlFor="source-sheet">
                  <Text type="label" display="block">
                    需求工作表
                  </Text>
                </label>
                <Select
                  id="source-sheet"
                  disabled={busy}
                  value={sheetName}
                  onChange={(e) => {
                    setSheetName(e.target.value);
                    setUploaded(null);
                  }}
                >
                  {inspection.sheets.map((w) => (
                    <option key={w.name}>{w.name}</option>
                  ))}
                </Select>
              </VStack>
              <VStack gap={3} style={{ minWidth: 0 }}>
                <label htmlFor="source-header">
                  <Text type="label" display="block">
                    列名所在行
                  </Text>
                </label>
                <Input
                  id="source-header"
                  type="number"
                  min={1}
                  max={100}
                  required
                  disabled={busy}
                  value={headerRow}
                  onChange={(e) => {
                    setHeaderRow(Number(e.target.value));
                    setUploaded(null);
                  }}
                />
              </VStack>
              <Text type="supporting" as="p">
                只导入所选工作表；月度文件需匹配日期列，仅取 {day} 的记录。
              </Text>
              <Collapsible
                defaultIsOpen={false}
                style={{ minWidth: 0, maxWidth: "100%", gridColumn: "1 / -1" }}
                trigger={
                  <Text type="body" weight="semibold">
                    预览工作表前10行
                  </Text>
                }
              >
                <VStack
                  gap={0}
                  width="100%"
                  style={{ minWidth: 0, maxWidth: "100%" }}
                >
                  <Table
                    density="balanced"
                    hasHover
                    dividers="rows"
                    style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                  >
                    <TableBody>
                      {inspection.sheets
                        .find((w) => w.name === sheetName)
                        ?.sample_rows.map((r) => (
                          <TableRow key={r.row}>
                            <TableHeaderCell>第{r.row}行</TableHeaderCell>
                            {r.values.map((v, i) => (
                              <TableCell key={i}>{v ?? "—"}</TableCell>
                            ))}
                          </TableRow>
                        ))}
                    </TableBody>
                  </Table>
                </VStack>
              </Collapsible>
            </Grid>
          )}
          <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
            <Action variant="secondary" disabled={busy || !file}>
              {busy ? <Spinner /> : <Upload size={14} />}{" "}
              {inspection ? "上传并读取所选列" : "读取工作表"}
            </Action>
            <Link
              isStandalone
              href="/api/planning/demand-template.xlsx"
              download
            >
              下载模板
            </Link>
            <Text type="body">最多5MiB / 10000行</Text>
          </HStack>
        </VStack>
        <ErrorMessage text={error} />
        {uploaded && (
          <>
            <Text type="supporting" as="p">
              工作表 {uploaded.source?.sheet_name ?? "数据"} · 第
              {uploaded.source?.header_row ?? 1}行表头 ·
              只读取已匹配字段，公式字段不计算。
            </Text>
            <Heading level={3}>
              列匹配 · 文件 {uploaded.source_row_count} 行
            </Heading>
            <Grid
              columns={{ minWidth: 220, max: 3 }}
              gap={4}
              style={{ minWidth: 0 }}
            >
              {(
                [
                  ["store_name", "门店名称列 *"],
                  ["pieces", "件数列 *"],
                  ["store_code", "门店编号列（可选）"],
                  ["delivery_date", "配送日期列（月度文件必选）"],
                ] as const
              ).map(([key, label]) => (
                <VStack gap={3} style={{ minWidth: 0 }} key={key}>
                  <label htmlFor={"mapping-" + key}>
                    <Text type="label" display="block">
                      {label}
                    </Text>
                  </label>
                  <Select
                    id={"mapping-" + key}
                    disabled={busy}
                    value={mapping[key]}
                    onChange={(e) => changeMapping(key, e.target.value)}
                  >
                    <option value="">
                      {key === "store_code"
                        ? "不使用编号列"
                        : key === "delivery_date"
                          ? "文件不含日期列"
                          : "请选择"}
                    </option>
                    {uploaded.columns.map((c) => (
                      <option key={c.index} value={c.index}>
                        {columnLabel(c.index)} · {c.label}
                      </option>
                    ))}
                  </Select>
                </VStack>
              ))}
            </Grid>
            <Collapsible
              defaultIsOpen={false}
              style={{ minWidth: 0, maxWidth: "100%", gridColumn: "1 / -1" }}
              trigger={
                <Text type="body" weight="semibold">
                  查看文件前几行，核对列匹配
                </Text>
              }
            >
              <VStack
                gap={0}
                width="100%"
                style={{ minWidth: 0, maxWidth: "100%" }}
              >
                <Table
                  density="balanced"
                  hasHover
                  dividers="rows"
                  style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                >
                  <TableHeader>
                    <TableRow>
                      <TableHeaderCell>原文件行</TableHeaderCell>
                      {uploaded.columns.map((c) => (
                        <TableHeaderCell key={c.index}>
                          {columnLabel(c.index)} · {c.label}
                        </TableHeaderCell>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {uploaded.sample_rows.slice(0, 5).map((r) => (
                      <TableRow key={r.row}>
                        <TableCell>{r.row}</TableCell>
                        {uploaded.columns.map((c) => (
                          <TableCell key={c.index}>
                            {r.values[c.index] === null ||
                            r.values[c.index] === undefined
                              ? "—"
                              : String(r.values[c.index])}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </VStack>
            </Collapsible>
            <Action
              variant="secondary"
              type="button"
              disabled={
                busy || mapping.store_name === "" || mapping.pieces === ""
              }
              onClick={() => void preview()}
            >
              {busy ? <Spinner /> : <RefreshCw size={14} />} 校验并预览
            </Action>
          </>
        )}
        {result && (
          <VStack gap={3} style={{ minWidth: 0 }}>
            <VStack gap={3} style={{ minWidth: 0 }}>
              <CheckCircle2 size={18} />
              {result.can_commit
                ? "校验通过，尚未替换当日需求"
                : "存在错误，整批未保存"}
            </VStack>
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Text type="body">数据 {result.source_row_count} 行</Text>
              <Text type="body">
                当日 {result.selected_date_row_count ?? result.source_row_count}{" "}
                行
              </Text>
              <Text type="body">
                其他日期 {result.other_date_row_count ?? 0} 行
              </Text>
              <Text type="body">
                模板尾行 {result.ignored_nonbusiness_row_count ?? 0} 行
              </Text>
              <Text type="body">合并重复 {result.merged_row_count} 行</Text>
              <Text type="body">跳过零件数 {result.zero_row_count} 行</Text>
              <Text type="body">空行 {result.blank_rows} 行</Text>
              <Text type="body">错误 {result.error_count} 项</Text>
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
                <VStack gap={3} style={{ minWidth: 0 }}>
                  <Text type="body">替换前</Text>
                  <Text type="label" weight="semibold" hasTabularNumbers>
                    {result.before.task_count}票 /{" "}
                    {number(result.before.total_pieces)}件
                  </Text>
                </VStack>
                <Text type="large" weight="semibold" hasTabularNumbers>
                  →
                </Text>
                <VStack gap={3} style={{ minWidth: 0 }}>
                  <Text type="body">
                    {result.can_commit
                      ? "确认后替换为"
                      : "有效行汇总（不可提交）"}
                  </Text>
                  <Text type="label" weight="semibold" hasTabularNumbers>
                    {result.after.task_count}票 /{" "}
                    {number(result.after.total_pieces)}件
                  </Text>
                </VStack>
              </VStack>
            </Section>
            {result.errors.length > 0 ? (
              <VStack gap={3} style={{ minWidth: 0 }}>
                <Table
                  density="balanced"
                  hasHover
                  dividers="rows"
                  style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                >
                  <TableHeader>
                    <TableRow>
                      <TableHeaderCell>来源行</TableHeaderCell>
                      <TableHeaderCell>字段</TableHeaderCell>
                      <TableHeaderCell>错误原因</TableHeaderCell>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {result.errors.map((e, i) => (
                      <TableRow key={i}>
                        <TableCell>{e.row}</TableCell>
                        <TableCell>{e.field}</TableCell>
                        <TableCell>{e.message}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {result.error_count > result.errors.length && (
                  <Text type="body" as="p">
                    仅展示前1000项错误，请分批修正。
                  </Text>
                )}
              </VStack>
            ) : (
              <>
                <Text type="supporting" as="p">
                  新增 {result.changes.added} 店，移除 {result.changes.removed}{" "}
                  店，件数或仓库变化 {result.changes.updated} 店。
                  {result.configuration_warning_count > 0
                    ? `${result.configuration_warning_count}家门店有配置提示，不阻止需求保存。`
                    : ""}
                </Text>
                <VStack gap={3} style={{ minWidth: 0 }}>
                  <Table
                    density="balanced"
                    hasHover
                    dividers="rows"
                    style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHeaderCell>出货仓</TableHeaderCell>
                        <TableHeaderCell>配送任务</TableHeaderCell>
                        <TableHeaderCell>件数</TableHeaderCell>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {result.after.warehouses.map((w) => (
                        <TableRow key={w.warehouse_id}>
                          <TableCell>{w.warehouse_name}</TableCell>
                          <TableCell>{w.task_count}</TableCell>
                          <TableCell>{number(w.total_pieces)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </VStack>
                <VStack gap={3} style={{ minWidth: 0 }}>
                  <Table
                    density="balanced"
                    hasHover
                    dividers="rows"
                    style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHeaderCell>门店</TableHeaderCell>
                        <TableHeaderCell>汇总件数</TableHeaderCell>
                        <TableHeaderCell>来源行</TableHeaderCell>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {result.items.slice(0, 100).map((t) => (
                        <TableRow key={t.store_id}>
                          <TableCell>
                            {t.store_code} · {t.store_name}
                          </TableCell>
                          <TableCell>{number(t.pieces)}</TableCell>
                          <TableCell>
                            <Action
                              variant="ghost"
                              onClick={() =>
                                setSources({
                                  name: t.store_name,
                                  rows: t.source_rows,
                                })
                              }
                            >
                              {t.source_rows
                                .slice(0, 10)
                                .map((s) => s.row)
                                .join("、")}
                              {t.source_rows.length > 10 ? "…" : ""}（
                              {t.source_rows.length}行）
                            </Action>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </VStack>
                {result.items.length > 100 && (
                  <Text type="supporting" as="p">
                    明细预览展示前100票，确认提交整批 {result.items.length} 票。
                  </Text>
                )}
                {result.zero_row_count > 0 && (
                  <Action
                    variant="ghost"
                    onClick={() =>
                      setSources({
                        name: "跳过的零件数行",
                        rows: result.zero_rows,
                      })
                    }
                  >
                    查看跳过的 {result.zero_row_count} 个零件数来源行
                  </Action>
                )}
                <label>
                  <Text type="label" display="block">
                    <Input
                      type="checkbox"
                      checked={confirmed}
                      disabled={busy}
                      onChange={(e) => setConfirmed(e.target.checked)}
                    />
                    <Text type="body">
                      我确认将 {day} 当前分组的需求替换为{" "}
                      {result.after.task_count}票、
                      {number(result.after.total_pieces)}件
                      {result.after.task_count === 0
                        ? "，清空当日配送任务"
                        : ""}
                      。
                    </Text>
                  </Text>
                </label>
              </>
            )}
          </VStack>
        )}
        <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
          <Action
            variant="secondary"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </Action>
          <Action
            variant="primary"
            type="button"
            disabled={busy || !result?.can_commit || !confirmed}
            onClick={() => void commit()}
          >
            {busy && <Spinner />}
            {result?.can_commit && result.after.task_count === 0
              ? "确认清空当日需求"
              : "确认替换当日需求"}
          </Action>
        </HStack>
      </Modal>
      {sources && (
        <SourceRows
          name={sources.name}
          rows={sources.rows}
          onClose={() => setSources(null)}
        />
      )}
    </>
  );
}
function DeadlineEditor({
  task,
  day,
  groupId,
  onClose,
  onSave,
}: {
  groupId?: string;
  task: DemandTask;
  day: string;
  onClose: () => void;
  onSave: () => void;
}) {
  const [value, setValue] = useState(
    task.latest_arrival_source === "BUSINESS_CLOSE"
      ? ""
      : (task.latest_arrival_at?.slice(0, 16) ?? ""),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const nextDay = new Date(Date.parse(day + "T00:00:00Z") + 86400000)
    .toISOString()
    .slice(0, 10);
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(
        "/planning/days/" +
          day +
          "/stores/" +
          task.store_id +
          (groupId ? "?group_id=" + encodeURIComponent(groupId) : ""),
        "PUT",
        {
          expected_revision: task.day_revision,
          latest_arrival_at: value ? value + ":00+08:00" : null,
        },
      );
      onSave();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="当日收货限制"
      description={day + " · " + task.store_name}
      onClose={onClose}
      busy={busy}
    >
      <VStack as="form" gap={4} onSubmit={save}>
        <label htmlFor="daily-deadline">
          <Text type="label" display="block">
            最晚实际到店时间（北京时间）
          </Text>
        </label>
        <Input
          id="daily-deadline"
          type="datetime-local"
          min={day + "T00:00"}
          max={nextDay + "T23:59"}
          value={value}
          onInput={(e) => setValue(e.currentTarget.value)}
          onChange={(e) => setValue(e.target.value)}
        />
        <Text type="supporting" as="p">
          营业结束时间：{clock(task.business_close_at)}
          。留空自动跟随门店营业结束时间，次日闭店会自动使用次日日期；有特殊要求时可单独填写当前配送日或次日。修改后需重新冻结输入。
        </Text>
        <Action
          variant="ghost"
          type="button"
          disabled={busy}
          onClick={() => setValue("")}
        >
          按营业结束时间
        </Action>
        <ErrorMessage text={error} />
        <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
          <Action
            variant="secondary"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </Action>
          <Action variant="primary" disabled={busy}>
            {busy && <Spinner />} 保存收货限制
          </Action>
        </HStack>
      </VStack>
    </Modal>
  );
}
function SnapshotHistory({
  day,
  groupId,
  onClose,
}: {
  groupId?: string;
  day: string;
  onClose: () => void;
}) {
  const [items, setItems] = useState<SnapshotMeta[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setLoading(true);
    api<{
      items: SnapshotMeta[];
      total: number;
    }>(
      "/planning/days/" +
        day +
        "/snapshots?offset=" +
        page * pageSize +
        "&limit=" +
        pageSize +
        (groupId ? "&group_id=" + encodeURIComponent(groupId) : ""),
    )
      .then((r) => {
        if (active) {
          setItems(r.items);
          setTotal(r.total);
        }
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [day, page, pageSize]);
  return (
    <>
      <Modal
        wide
        title="历史规划输入"
        description={day + " · 只读版本"}
        onClose={onClose}
      >
        <ErrorMessage text={error} />
        {loading ? (
          <VStack
            gap={3}
            paddingBlock={6}
            align="center"
            style={{ minWidth: 0, textAlign: "center" }}
          >
            <Spinner /> 正在读取历史
          </VStack>
        ) : (
          <>
            <VStack
              gap={0}
              width="100%"
              style={{ minWidth: 0, maxWidth: "100%" }}
            >
              <Table
                density="balanced"
                hasHover
                dividers="rows"
                style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
              >
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>版本</TableHeaderCell>
                    <TableHeaderCell>需求</TableHeaderCell>
                    <TableHeaderCell>保存时间</TableHeaderCell>
                    <TableHeaderCell>当前状态</TableHeaderCell>
                    <TableHeaderCell>操作</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((s) => (
                    <TableRow key={s.id}>
                      <TableCell>V{s.version}</TableCell>
                      <TableCell>
                        {s.totals.task_count}票 /{" "}
                        {number(s.totals.total_pieces)}件
                      </TableCell>
                      <TableCell>{clock(s.created_at)}</TableCell>
                      <TableCell>
                        {s.input_changed ? "输入已变更" : "与当前一致"}
                      </TableCell>
                      <TableCell>
                        <Action
                          variant="ghost"
                          onClick={() => setSelected(s.id)}
                        >
                          查看快照
                        </Action>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </VStack>
            <Pagination
              label="历史快照"
              page={page}
              total={total}
              pageSize={pageSize}
              disabled={loading}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(0);
              }}
            />
          </>
        )}
      </Modal>
      {selected && (
        <SnapshotViewer id={selected} onClose={() => setSelected(null)} />
      )}
    </>
  );
}
function SnapshotViewer({ id, onClose }: { id: string; onClose: () => void }) {
  const [snapshot, setSnapshot] = useState<SnapshotDetail | null>(null);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  const [sources, setSources] = useState<{
    name: string;
    rows: Source[];
  } | null>(null);
  useEffect(() => {
    let active = true;
    api<SnapshotDetail>("/planning/snapshots/" + id)
      .then((r) => {
        if (active) setSnapshot(r);
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      });
    return () => {
      active = false;
    };
  }, [id]);
  const stores = new Map(snapshot?.input.stores.map((s) => [s.id, s] as const));
  const warehouseNames = new Map(
    snapshot?.input.warehouses.map((w) => [w.id, w.name] as const),
  );
  return (
    <>
      <Modal
        wide
        title={snapshot ? "规划输入 V" + snapshot.version : "规划输入快照"}
        description={
          snapshot
            ? snapshot.delivery_date + " · 已冻结内容，只读查看"
            : "正在读取"
        }
        onClose={onClose}
      >
        <ErrorMessage text={error} />
        {!snapshot && !error && (
          <VStack
            gap={3}
            paddingBlock={6}
            align="center"
            style={{ minWidth: 0, textAlign: "center" }}
          >
            <Spinner /> 正在读取冻结内容
          </VStack>
        )}
        {snapshot && (
          <>
            <VStack gap={3} style={{ minWidth: 0 }}>
              {snapshot.input_changed
                ? "输入已变更；以下仍为保存时的原始内容"
                : "该快照与当前输入一致"}
            </VStack>
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Text type="label" weight="semibold" hasTabularNumbers>
                {snapshot.totals.task_count}票 ·{" "}
                {number(snapshot.totals.total_pieces)}件 · 需求R
                {snapshot.demand_revision}
              </Text>
              <Text type="body">
                {clock(snapshot.created_at)} · {snapshot.created_by}
              </Text>
              <Text
                type="code"
                display="block"
                style={{ overflowWrap: "anywhere" }}
              >
                {snapshot.content_hash}
              </Text>
              <Link
                isStandalone
                href={"/api/planning/snapshots/" + id + "/export.json"}
                download
              >
                <HStack as="span" gap={2} vAlign="center">
                  <Download size={13} /> 下载输入JSON
                </HStack>
              </Link>
            </HStack>
            <VStack
              gap={0}
              width="100%"
              style={{ minWidth: 0, maxWidth: "100%" }}
            >
              <Table
                density="balanced"
                hasHover
                dividers="rows"
                style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
              >
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>冻结门店 / 仓库</TableHeaderCell>
                    <TableHeaderCell>件数 / 来源</TableHeaderCell>
                    <TableHeaderCell>经营时间 / 卸货</TableHeaderCell>
                    <TableHeaderCell>最晚到店</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {snapshot.input.tasks
                    .slice(page * pageSize, page * pageSize + pageSize)
                    .map((t) => {
                      const store = stores.get(t.store_id);
                      return (
                        <TableRow key={t.id}>
                          <TableCell>
                            <Text
                              type="label"
                              weight="semibold"
                              hasTabularNumbers
                            >
                              {t.store_name}
                            </Text>
                            <Text type="supporting" display="block">
                              {t.store_code} · {t.warehouse_name}
                            </Text>
                          </TableCell>
                          <TableCell>
                            {number(t.pieces)} 件<br />
                            <Action
                              variant="ghost"
                              onClick={() =>
                                setSources({
                                  name: t.store_name,
                                  rows: t.source_rows,
                                })
                              }
                            >
                              {t.source_rows.length} 行来源
                            </Action>
                          </TableCell>
                          <TableCell>
                            {clock(store?.business_open_at)}
                            <br />至 {clock(store?.business_close_at)}
                            <Text type="supporting" display="block">
                              卸货 {String(store?.unload_minutes ?? 0)} 分钟
                            </Text>
                          </TableCell>
                          <TableCell>{clock(t.latest_arrival_at)}</TableCell>
                        </TableRow>
                      );
                    })}
                </TableBody>
              </Table>
            </VStack>
            <Pagination
              label="快照明细"
              page={page}
              total={snapshot.input.tasks.length}
              pageSize={pageSize}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(0);
              }}
            />
            <Collapsible
              defaultIsOpen={false}
              style={{ minWidth: 0, maxWidth: "100%", gridColumn: "1 / -1" }}
              trigger={
                <Text type="body" weight="semibold">
                  冻结仓库与车辆 · {snapshot.input.warehouses.length}仓 /{" "}
                  {snapshot.input.vehicles.length}车
                </Text>
              }
            >
              <HStack
                gap={3}
                wrap="wrap"
                align="center"
                style={{ minWidth: 0 }}
              >
                {snapshot.input.warehouses.map((w) => (
                  <Text type="body" as="p" key={w.id}>
                    <Text type="label" weight="semibold" hasTabularNumbers>
                      {w.name}
                    </Text>{" "}
                    · {clock(String(w.earliest_departure_at))} 发车
                  </Text>
                ))}
              </HStack>
              <VStack
                gap={0}
                width="100%"
                style={{ minWidth: 0, maxWidth: "100%" }}
              >
                <Table
                  density="balanced"
                  hasHover
                  dividers="rows"
                  style={{ minWidth: "calc(var(--spacing-10) * 14)" }}
                >
                  <TableHeader>
                    <TableRow>
                      <TableHeaderCell>车牌</TableHeaderCell>
                      <TableHeaderCell>当天归仓</TableHeaderCell>
                      <TableHeaderCell>件数容量</TableHeaderCell>
                      <TableHeaderCell>可用性</TableHeaderCell>
                      <TableHeaderCell>道路资料</TableHeaderCell>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {snapshot.input.vehicles.map((v) => (
                      <TableRow key={v.id}>
                        <TableCell>{v.plate}</TableCell>
                        <TableCell>
                          {String(
                            warehouseNames.get(String(v.day_warehouse_id)),
                          )}
                        </TableCell>
                        <TableCell>{String(v.max_pieces)}</TableCell>
                        <TableCell>
                          {v.day_available ? "可用" : "不可用"}
                        </TableCell>
                        <TableCell>
                          {v.road_profile_complete
                            ? "资料齐全，待验证"
                            : (v.road_profile_missing ?? []).join("、")}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </VStack>
            </Collapsible>
            <Collapsible
              defaultIsOpen={false}
              style={{ minWidth: 0, maxWidth: "100%", gridColumn: "1 / -1" }}
              trigger={
                <Text type="body" weight="semibold">
                  冻结有效报价 · {snapshot.input.quotes.length}个版本
                </Text>
              }
            >
              {snapshot.input.quotes.map((q) => (
                <VStack gap={3} style={{ minWidth: 0 }} key={q.id}>
                  <Text type="label" weight="semibold" hasTabularNumbers>
                    {q.name} · V{q.version}
                  </Text>
                  <Text type="body" as="p">
                    {quoteDescription(q)}
                  </Text>
                </VStack>
              ))}
              <VStack
                gap={0}
                width="100%"
                style={{ minWidth: 0, maxWidth: "100%" }}
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
                      <TableHeaderCell>共配每件单价</TableHeaderCell>
                      <TableHeaderCell>专车报价版本</TableHeaderCell>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {snapshot.input.stores.map((s) => (
                      <TableRow key={s.id}>
                        <TableCell>
                          {s.code} · {s.name}
                        </TableCell>
                        <TableCell>
                          {s.common_unit_price === null
                            ? "未配置"
                            : String(s.common_unit_price) + " 元"}
                        </TableCell>
                        <TableCell>
                          {s.dedicated_quote_version
                            ? s.dedicated_quote_version.name +
                              " · V" +
                              s.dedicated_quote_version.version
                            : "未配置有效报价"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </VStack>
            </Collapsible>
            <Text type="supporting" as="p">
              道路状态：尚未验证。本快照用于后续计费、货车道路和排线，不能作为已确认执行线路。
            </Text>
          </>
        )}
      </Modal>
      {sources && (
        <SourceRows
          name={sources.name}
          rows={sources.rows}
          onClose={() => setSources(null)}
        />
      )}
    </>
  );
}
function ManualDemandDialog({
  groupId,
  current,
  catalog,
  mode,
  task,
  onClose,
  onSave,
}: {
  groupId: string;
  current: DayData | null;
  catalog: Catalog;
  mode: "new" | "edit" | "delete";
  task?: DemandTask;
  onClose: () => void;
  onSave: () => void;
}) {
  const [storeId, setStoreId] = useState(task?.store_id ?? "");
  const [pieces, setPieces] = useState(String(task?.pieces ?? 1));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const existing = new Set(current?.items.map((r) => r.store_id));
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const items = (current?.items ?? [])
        .filter((r) => r.store_id !== storeId)
        .map((r) => ({ store_id: r.store_id, pieces: r.pieces }));
      if (mode !== "delete")
        items.push({ store_id: storeId, pieces: Number(pieces) });
      await api("/planning/groups/" + groupId + "/demands", "PUT", {
        expected_revision: current?.revision ?? 0,
        items,
      });
      onSave();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={
        mode === "new"
          ? "新建分组需求"
          : mode === "edit"
            ? "修改需求件数"
            : "移除分组需求"
      }
      description={task?.store_name ?? "选择门店并填写配送件数"}
      onClose={onClose}
      busy={busy}
    >
      <VStack as="form" gap={4} onSubmit={save}>
        {mode === "delete" ? (
          <Text type="body" as="p">
            将从当前分组移除 {task?.store_name}{" "}
            的需求并生成新快照。原有快照和方案保留为历史版本。
          </Text>
        ) : (
          <>
            <label htmlFor="manual-demand-store">
              <Text type="label" display="block">
                门店
              </Text>
            </label>
            <Select
              id="manual-demand-store"
              required
              value={storeId}
              disabled={mode === "edit"}
              onChange={(e) => setStoreId(e.target.value)}
            >
              <option value="">请选择门店</option>
              {catalog.stores
                .filter((s) => !existing.has(s.id) || s.id === task?.store_id)
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.code} · {s.name} · {String(s.warehouse_name)}
                  </option>
                ))}
            </Select>
            <label htmlFor="manual-demand-pieces">
              <Text type="label" display="block">
                配送件数
              </Text>
            </label>
            <Input
              id="manual-demand-pieces"
              type="number"
              min={1}
              max={1000000000}
              step={1}
              required
              value={pieces}
              onChange={(e) => setPieces(e.target.value)}
            />
          </>
        )}
        <ErrorMessage text={error} />
        <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
          <Action
            variant="secondary"
            type="button"
            onClick={onClose}
            disabled={busy}
          >
            取消
          </Action>
          <Action variant="primary" disabled={busy}>
            {busy && <Spinner />}
            {mode === "delete" ? "确认移除" : "保存需求"}
          </Action>
        </HStack>
      </VStack>
    </Modal>
  );
}
