import { VStack } from "@astryxdesign/core/VStack";
import { HStack } from "@astryxdesign/core/HStack";
import { Grid } from "@astryxdesign/core/Grid";
import { Section } from "@astryxdesign/core/Section";
import { Text } from "@astryxdesign/core/Text";
import { Heading } from "@astryxdesign/core/Heading";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHeaderCell,
  TableCell,
} from "@astryxdesign/core/Table";
import { Action, Input, Select } from "./ui";
import React, { FormEvent, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  FileSpreadsheet,
  GitCompareArrows,
  MapPin,
  RefreshCw,
  Route,
  Truck,
  Warehouse,
} from "lucide-react";
import { api } from "./api";
import {
  Catalog,
  dateToday,
  quoteDescription,
  Row,
  QuoteVersion,
} from "./master-data";
import { ErrorMessage, Modal, Spinner } from "./ui";
import { LocationMap } from "./location-map";
import { Pagination, PageSize } from "./pagination";
import { DemandPanel, DayData } from "./demand-panel";
import { PlanPanel } from "./plan-panel";
import { editorLeaveDecision } from "./unsaved";
type DemandGroup = {
  id: string;
  name: string;
  delivery_date: string;
  revision: number;
  task_count: number;
  total_pieces: number;
  snapshot_count: number;
  plan_count: number;
};
const empty: Catalog = {
  warehouses: [],
  stores: [],
  vehicles: [],
  quotes: [],
  master_revision: 0,
};
export function PlanningPage({
  notify,
  initialDay,
}: {
  notify: (message: string) => void;
  initialDay?: string;
}) {
  const [groups, setGroups] = useState<DemandGroup[]>([]);
  const [groupId, setGroupId] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [groupBusy, setGroupBusy] = useState(false);
  const [groupError, setGroupError] = useState("");
  const group = groups.find((g) => g.id === groupId);
  const [day, setDay] = useState(() => initialDay ?? dateToday());
  const [demand, setDemand] = useState<DayData | null>(null);
  const [section, setSection] = useState<
    "demand" | "resources" | "checks" | "plans"
  >("demand");
  const [data, setData] = useState<Catalog>(empty);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<PageSize>(10);
  const [selected, setSelected] = useState<string[]>([]);
  const [comparing, setComparing] = useState(false);
  const [showMap, setShowMap] = useState(false);
  const [editing, setEditing] = useState<{
    kind: "warehouses" | "vehicles";
    row: Row;
  } | null>(null);
  const generation = useRef(0);
  const dayRef = useRef(day);
  dayRef.current = day;
  const pendingDay = useRef(false);
  async function load() {
    const request = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const [groupList, result, daily] = await Promise.all([
        api<{
          items: DemandGroup[];
        }>("/planning/days/" + day + "/groups"),
        api<Catalog>(
          groupId
            ? "/planning/groups/" + groupId + "/catalog"
            : "/master-data/catalog?delivery_date=" + day,
        ),
        groupId
          ? api<DayData>("/planning/days/" + day + "?group_id=" + groupId)
          : Promise.resolve(null),
      ]);
      if (request === generation.current) {
        setGroups(groupList.items);
        setData(result);
        setDemand(daily);
      }
    } catch (e) {
      if (request === generation.current) {
        setError(e instanceof Error ? e.message : "读取业务资料失败。");
        setData(empty);
        setDemand(null);
      }
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }
  useEffect(() => {
    setSelected([]);
    void load();
    return () => {
      generation.current++;
    };
  }, [day, groupId]);
  async function chooseGroup(value: string) {
    if (value === groupId) return;
    const decision = editorLeaveDecision();
    if (await decision) {
      setGroupId(value);
      setDemand(null);
      setSelected([]);
    }
  }
  async function createGroup(e: FormEvent) {
    e.preventDefault();
    setGroupBusy(true);
    setGroupError("");
    try {
      const item = await api<DemandGroup>(
        "/planning/days/" + day + "/groups",
        "POST",
        { name: groupName },
      );
      setGroups((v) => [...v, item]);
      setCreating(false);
      setGroupName("");
      setGroupId(item.id);
      notify("已创建需求分组 " + item.name);
    } catch (e) {
      setGroupError(errorText(e));
    } finally {
      setGroupBusy(false);
    }
  }
  async function removeGroup() {
    if (!group) return;
    setGroupBusy(true);
    setGroupError("");
    try {
      await api(
        "/planning/groups/" + group.id + "?expected_revision=" + group.revision,
        "DELETE",
      );
      setDeleting(false);
      setGroups((v) => v.filter((g) => g.id !== group.id));
      setGroupId("");
      setDemand(null);
      notify("分组及组内需求、快照和方案已删除");
    } catch (e) {
      setGroupError(errorText(e));
    } finally {
      setGroupBusy(false);
    }
  }
  const warehouses = data.warehouses.filter(
    (w) => !warehouse || w.id === warehouse,
  );
  const vehicles = data.vehicles.filter(
    (v) => !warehouse || v.day_warehouse_id === warehouse,
  );
  const demanded = new Set(
    demand?.delivery_date === day ? demand.items.map((t) => t.store_id) : [],
  );
  const stores = data.stores.filter(
    (s) =>
      demanded.has(s.id) &&
      (!warehouse || s.warehouse_id === warehouse) &&
      [s.code, s.name, s.city].some((v) => String(v ?? "").includes(query)),
  );
  const pages = Math.ceil(stores.length / pageSize);
  const current = Math.min(page, Math.max(pages - 1, 0));
  const visible = stores.slice(
    current * pageSize,
    current * pageSize + pageSize,
  );
  function finish() {
    setEditing(null);
    notify("当日安排已保存，输入有变化时请重新冻结快照");
    void load();
  }
  function changeDay(input: HTMLInputElement) {
    const value = input.value;
    const previous = dayRef.current;
    if (!value || value === previous) {
      if (!value) input.value = previous;
      return;
    }
    if (pendingDay.current) {
      input.value = previous;
      return;
    }
    const commit = () => {
      dayRef.current = value;
      setDay(value);
      setGroupId("");
      setDemand(null);
    };
    const decision = editorLeaveDecision();
    // A controlled date input must update synchronously when no editor is
    // dirty. The ref deduplicates input/change before React commits its render.
    if (typeof decision === "boolean") {
      if (decision) commit();
      else input.value = previous;
    } else {
      pendingDay.current = true;
      input.value = previous;
      void decision.then((accepted) => {
        pendingDay.current = false;
        if (accepted) commit();
        else input.value = dayRef.current;
      });
    }
  }
  return (
    <VStack gap={6} width="100%" style={{ minWidth: 0 }}>
      <HStack
        gap={3}
        wrap="wrap"
        align="center"
        justify="between"
        style={{ minWidth: 0 }}
      >
        <VStack gap={3} style={{ minWidth: 0 }}>
          <Text type="supporting" weight="semibold" color="accent">
            LOGISTICS PLANNING
          </Text>
          <Heading level={1}>物流线路规划</Heading>
          <Text type="supporting" as="p">
            导入每日需求，核对资源，联合排线并保留人工调整版本。
          </Text>
        </VStack>
        <Action
          variant="secondary"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw size={15} /> 刷新业务资料
        </Action>
      </HStack>
      <Grid columns={{ minWidth: 220, max: 3 }} gap={4} style={{ minWidth: 0 }}>
        <VStack gap={3} style={{ minWidth: 0 }}>
          <label htmlFor="planning-date">
            <Text type="label" display="block">
              配送日期
            </Text>
          </label>
          <Input
            id="planning-date"
            type="date"
            required
            value={day}
            onInput={(e) => changeDay(e.currentTarget)}
            onChange={(e) => changeDay(e.currentTarget)}
          />
        </VStack>
        <VStack gap={3} style={{ minWidth: 0 }}>
          <label htmlFor="planning-warehouse">
            <Text type="label" display="block">
              出货仓库
            </Text>
          </label>
          <Select
            id="planning-warehouse"
            value={warehouse}
            onChange={(e) => {
              setWarehouse(e.target.value);
              setPage(0);
            }}
          >
            <option value="">全部仓库</option>
            {data.warehouses.map((w) => (
              <option key={w.id} value={w.id}>
                {w.code} · {w.name}
              </option>
            ))}
          </Select>
        </VStack>
        <Text type="body" as="p">
          本页的安排按配送日期保存，基础资料继续统一维护。
        </Text>
      </Grid>
      <Grid columns={{ minWidth: 220, max: 3 }} gap={4} style={{ minWidth: 0 }}>
        <VStack gap={3} style={{ minWidth: 0 }}>
          <label htmlFor="demand-group">
            <Text type="label" display="block">
              需求分组
            </Text>
          </label>
          <Select
            id="demand-group"
            value={groupId}
            disabled={loading}
            onChange={(e) => void chooseGroup(e.target.value)}
          >
            <option value="">请选择需求分组</option>
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name} · {g.task_count}票 / {g.total_pieces}件
              </option>
            ))}
          </Select>
        </VStack>
        <HStack gap={2} wrap="wrap" align="end">
          <Action
            variant="primary"
            onClick={() => {
              setGroupError("");
              setCreating(true);
            }}
          >
            新建需求分组
          </Action>
          <Action
            variant="secondary"
            disabled={!group || loading}
            onClick={() => {
              setGroupError("");
              setDeleting(true);
            }}
          >
            删除分组
          </Action>
        </HStack>
        <Text type="body" as="p">
          需求、收货限制、资源安排、快照和方案均按分组保存。
        </Text>
      </Grid>
      <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
        <Text type="body">
          <HStack as="span" gap={2} vAlign="center">
            <FileSpreadsheet size={15} /> 导入每日需求
          </HStack>
        </Text>
        <ArrowRight size={13} />
        <Text type="body">
          <HStack as="span" gap={2} vAlign="center">
            <Truck size={15} /> 核对当日资源
          </HStack>
        </Text>
        <ArrowRight size={13} />
        <Text type="body">
          <HStack as="span" gap={2} vAlign="center">
            <Route size={15} /> 冻结规划输入
          </HStack>
        </Text>
      </HStack>
      <ErrorMessage text={error} />
      {loading && (
        <VStack
          gap={3}
          paddingBlock={6}
          align="center"
          style={{ minWidth: 0, textAlign: "center" }}
        >
          <Spinner /> 正在读取 {day} 的业务资料
        </VStack>
      )}
      <HStack as="nav" gap={2} wrap="wrap" aria-label="物流业务板块">
        <Action
          onClick={() => setSection("demand")}
          variant={section === "demand" ? "primary" : "secondary"}
        >
          <FileSpreadsheet size={15} /> 每日需求与快照
        </Action>
        <Action
          onClick={() => setSection("resources")}
          variant={section === "resources" ? "primary" : "secondary"}
        >
          <Truck size={15} /> 仓库与车辆安排
        </Action>
        <Action
          onClick={() => setSection("checks")}
          variant={section === "checks" ? "primary" : "secondary"}
        >
          <GitCompareArrows size={15} /> 门店与报价校验
        </Action>
        <Action
          onClick={() => setSection("plans")}
          variant={section === "plans" ? "primary" : "secondary"}
        >
          <Route size={15} /> 方案规划与调整
        </Action>
      </HStack>
      {!groupId && (
        <VStack
          gap={3}
          paddingBlock={6}
          align="center"
          style={{ minWidth: 0, textAlign: "center" }}
        >
          请先选择或新建需求分组，再创建需求、校验报价或规划线路。
        </VStack>
      )}
      {groupId && (
        <React.Fragment key={day + groupId}>
          {section !== "demand" && demand?.latest_snapshot?.input_changed && (
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Text type="body">
                输入已变更：{demand.latest_snapshot.changed_reasons.join("；")}
              </Text>
              <Action variant="ghost" onClick={() => setSection("demand")}>
                查看并重新冻结
              </Action>
            </HStack>
          )}
          {section === "demand" && (
            <DemandPanel
              key={day + groupId}
              groupId={groupId}
              catalog={data}
              day={day}
              data={demand}
              loading={loading}
              onReload={() => void load()}
              notify={notify}
            />
          )}
          <VStack
            gap={4}
            hidden={section !== "plans"}
            style={{
              minWidth: 0,
              display: section !== "plans" ? "none" : undefined,
            }}
          >
            <PlanPanel
              key={day + groupId}
              groupId={groupId}
              day={day}
              demand={demand}
              notify={notify}
            />
          </VStack>
          {(section === "resources" || section === "checks") && (
            <>
              <Grid
                columns={{ minWidth: 180, max: 3 }}
                gap={4}
                style={{ minWidth: 0 }}
              >
                <VStack gap={2} padding={3}>
                  <Text type="body">出货仓库</Text>
                  <Text type="large" weight="semibold" hasTabularNumbers>
                    {warehouses.length}
                    <Text type="supporting"> 个</Text>
                  </Text>
                </VStack>
                <VStack gap={2} padding={3}>
                  <Text type="body">当日可用车辆</Text>
                  <Text type="large" weight="semibold" hasTabularNumbers>
                    {vehicles.filter((v) => v.day_available).length}
                    <Text type="supporting"> 辆</Text>
                  </Text>
                </VStack>
                <VStack gap={2} padding={3}>
                  <Text type="body">关联门店</Text>
                  <Text type="large" weight="semibold" hasTabularNumbers>
                    {stores.length}
                    <Text type="supporting"> 家</Text>
                  </Text>
                </VStack>
              </Grid>
              {section === "resources" && (
                <Grid
                  columns={{ minWidth: 320, max: 2 }}
                  gap={4}
                  style={{ minWidth: 0 }}
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
                          <Warehouse size={17} />
                          <Heading level={2}>仓库发车安排</Heading>
                        </HStack>
                      </HStack>
                      <VStack gap={3} style={{ minWidth: 0 }}>
                        {warehouses.map((w) => (
                          <HStack
                            gap={3}
                            wrap="wrap"
                            align="center"
                            justify="between"
                            style={{ minWidth: 0 }}
                            key={w.id}
                          >
                            <VStack gap={3} style={{ minWidth: 0 }}>
                              <Text
                                type="label"
                                weight="semibold"
                                hasTabularNumbers
                              >
                                {w.name}
                              </Text>
                              <Text type="supporting" display="block">
                                {w.code} ·{" "}
                                {w.day_override ? "当日单独安排" : "沿用默认"}
                              </Text>
                            </VStack>
                            <Text
                              type="large"
                              weight="semibold"
                              hasTabularNumbers
                            >
                              {String(w.day_departure_time)}
                            </Text>
                            <Action
                              variant="ghost"
                              disabled={loading}
                              onClick={() =>
                                setEditing({ kind: "warehouses", row: w })
                              }
                            >
                              设置发车
                            </Action>
                          </HStack>
                        ))}
                        {!warehouses.length && !loading && (
                          <Text type="body" as="p">
                            请先在仓库管理维护仓库资料。
                          </Text>
                        )}
                      </VStack>
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
                        <HStack
                          gap={3}
                          wrap="wrap"
                          align="center"
                          style={{ minWidth: 0 }}
                        >
                          <Truck size={17} />
                          <Heading level={2}>当日车辆安排</Heading>
                        </HStack>
                      </HStack>
                      <VStack gap={3} style={{ minWidth: 0 }}>
                        {vehicles.map((v) => (
                          <HStack
                            gap={3}
                            wrap="wrap"
                            align="center"
                            justify="between"
                            style={{ minWidth: 0 }}
                            key={v.id}
                          >
                            <VStack gap={3} style={{ minWidth: 0 }}>
                              <Text
                                type="label"
                                weight="semibold"
                                hasTabularNumbers
                              >
                                {v.plate}
                              </Text>
                              <Text type="supporting" display="block">
                                {String(v.day_warehouse_name)} ·{" "}
                                {String(v.max_pieces)} 件 ·{" "}
                                {v.day_available ? "可用" : "不可用"}
                              </Text>
                            </VStack>
                            <Text type="body">
                              <HStack
                                width="var(--spacing-1)"
                                height="var(--spacing-1)"
                                aria-hidden="true"
                                style={{
                                  background: "var(--color-text-secondary)",
                                  borderRadius: "var(--radius-full)",
                                }}
                              />
                              {v.day_override ? "单独安排" : "默认安排"}
                            </Text>
                            <Action
                              variant="ghost"
                              disabled={loading}
                              onClick={() =>
                                setEditing({ kind: "vehicles", row: v })
                              }
                            >
                              安排车辆
                            </Action>
                          </HStack>
                        ))}
                        {!vehicles.length && !loading && (
                          <Text type="body" as="p">
                            当前仓库没有车辆，请检查默认归仓或切换全部仓库。
                          </Text>
                        )}
                      </VStack>
                    </VStack>
                  </Section>
                </Grid>
              )}
              {section === "checks" && (
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
                        <Heading level={2}>门店与报价校验</Heading>
                        <Text type="body" color="accent">
                          {stores.length}
                        </Text>
                      </HStack>
                      <HStack
                        gap={3}
                        wrap="wrap"
                        align="center"
                        style={{ minWidth: 0 }}
                      >
                        <Action
                          variant="secondary"
                          onClick={() => setShowMap(!showMap)}
                        >
                          <MapPin size={14} />
                          {showMap ? "收起位置地图" : "查看位置地图"}
                        </Action>
                        <Input
                          aria-label="搜索规划门店"
                          placeholder="搜索编号、门店或城市"
                          value={query}
                          onChange={(e) => {
                            setQuery(e.target.value);
                            setPage(0);
                          }}
                        />
                      </HStack>
                    </HStack>
                    {showMap && (
                      <VStack gap={3} style={{ minWidth: 0, width: "100%" }}>
                        <LocationMap
                          points={[...warehouses, ...stores].map((r) => ({
                            id: r.id,
                            name: String(r.name),
                            code: String(r.code),
                            address: r.address,
                            longitude: r.longitude,
                            latitude: r.latitude,
                            crs: r.crs,
                          }))}
                          label="规划仓库与门店位置"
                        />
                      </VStack>
                    )}
                    <HStack
                      gap={3}
                      wrap="wrap"
                      align="center"
                      style={{ minWidth: 0 }}
                    >
                      <Text type="body">
                        已选择 {selected.length} 家门店（含其他页）
                      </Text>
                      {selected.length > 10000 && (
                        <Text type="body">
                          每次最多校验10000家门店，请减少选择。
                        </Text>
                      )}
                      <Action
                        variant="primary"
                        disabled={
                          loading ||
                          selected.length < 2 ||
                          selected.length > 10000
                        }
                        onClick={() => setComparing(true)}
                      >
                        <GitCompareArrows size={15} /> 判断报价兼容
                      </Action>
                      {selected.length > 0 && (
                        <Action variant="ghost" onClick={() => setSelected([])}>
                          清空选择
                        </Action>
                      )}
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
                            <TableHeaderCell>
                              <HStack
                                gap={3}
                                wrap="wrap"
                                align="center"
                                style={{ minWidth: 0 }}
                              >
                                <Input
                                  type="checkbox"
                                  aria-label="选择本页规划门店"
                                  checked={
                                    visible.length > 0 &&
                                    visible.every((s) =>
                                      selected.includes(s.id),
                                    )
                                  }
                                  disabled={loading || !visible.length}
                                  onChange={(e) =>
                                    setSelected(
                                      e.target.checked
                                        ? Array.from(
                                            new Set([
                                              ...selected,
                                              ...visible.map((s) => s.id),
                                            ]),
                                          )
                                        : selected.filter(
                                            (id) =>
                                              !visible.some((s) => s.id === id),
                                          ),
                                    )
                                  }
                                />
                                <Action
                                  variant="ghost"
                                  type="button"
                                  aria-label={
                                    "全选所有匹配规划门店（" +
                                    stores.length +
                                    "条）"
                                  }
                                  disabled={loading || !stores.length}
                                  onClick={() =>
                                    setSelected((previous) =>
                                      Array.from(
                                        new Set([
                                          ...previous,
                                          ...stores.map((s) => s.id),
                                        ]),
                                      ),
                                    )
                                  }
                                >
                                  全选所有（{stores.length}条匹配）
                                </Action>
                              </HStack>
                            </TableHeaderCell>
                            <TableHeaderCell>门店 / 出货仓</TableHeaderCell>
                            <TableHeaderCell>专车报价</TableHeaderCell>
                            <TableHeaderCell>共配每件价</TableHeaderCell>
                            <TableHeaderCell>配置提示</TableHeaderCell>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {visible.map((s) => (
                            <TableRow key={s.id}>
                              <TableCell>
                                <Input
                                  type="checkbox"
                                  aria-label={"选择规划门店 " + s.code}
                                  checked={selected.includes(s.id)}
                                  onChange={(e) =>
                                    setSelected(
                                      e.target.checked
                                        ? [...selected, s.id]
                                        : selected.filter((id) => id !== s.id),
                                    )
                                  }
                                />
                              </TableCell>
                              <TableCell>
                                <Text
                                  type="label"
                                  weight="semibold"
                                  hasTabularNumbers
                                >
                                  {s.name}
                                </Text>
                                <Text type="supporting" display="block">
                                  {s.code} · {String(s.warehouse_name)}
                                </Text>
                              </TableCell>
                              <TableCell>
                                {s.dedicated_quote_available
                                  ? quoteDescription(s.dedicated_quote_version)
                                  : "未配置有效报价"}
                              </TableCell>
                              <TableCell>
                                {s.common_unit_price === null
                                  ? "未配置"
                                  : s.common_unit_price + " 元"}
                              </TableCell>
                              <TableCell>
                                {s.dedicated_config_ready
                                  ? "基础配置齐全"
                                  : (s.dedicated_config_missing ?? []).join(
                                      "；",
                                    )}
                                {!s.common_available && (
                                  <Text type="supporting" display="block">
                                    共配缺价
                                  </Text>
                                )}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </VStack>
                    {!stores.length && !loading && (
                      <VStack
                        gap={3}
                        paddingBlock={6}
                        align="center"
                        style={{ minWidth: 0, textAlign: "center" }}
                      >
                        当日没有匹配的需求门店，请先导入每日需求。
                      </VStack>
                    )}
                    <Pagination
                      label="规划门店"
                      total={stores.length}
                      page={current}
                      pageSize={pageSize}
                      disabled={loading}
                      onPageChange={setPage}
                      onPageSizeChange={(size) => {
                        setPageSize(size);
                        setPage(0);
                      }}
                    />
                  </VStack>
                </Section>
              )}
              <Text type="supporting" as="p">
                报价兼容只表示同仓与计费参数一致。进入“方案规划与调整”进行排线、道路复核及人工确认。
              </Text>
            </>
          )}
        </React.Fragment>
      )}
      {creating && (
        <Modal
          title="新建需求分组"
          description={day}
          onClose={() => setCreating(false)}
          busy={groupBusy}
        >
          <VStack as="form" gap={4} onSubmit={createGroup}>
            <label htmlFor="new-group-name">
              <Text type="label" display="block">
                分组名称
              </Text>
            </label>
            <Input
              id="new-group-name"
              required
              maxLength={120}
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
            />
            <ErrorMessage text={groupError} />
            <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
              <Action
                variant="secondary"
                type="button"
                onClick={() => setCreating(false)}
              >
                取消
              </Action>
              <Action variant="primary" disabled={groupBusy}>
                创建分组
              </Action>
            </HStack>
          </VStack>
        </Modal>
      )}
      {deleting && group && (
        <Modal
          title="删除需求分组"
          description={`${day} · ${group.name}`}
          onClose={() => setDeleting(false)}
          busy={groupBusy}
        >
          <Text type="body" as="p">
            将删除本组 {group.task_count} 票需求、{group.snapshot_count}{" "}
            份快照、{group.plan_count}{" "}
            个方案版本和组内安排。基础资料和其他分组保留。此操作无法在页面撤销。
          </Text>
          <ErrorMessage text={groupError} />
          <HStack gap={3} wrap="wrap" align="center" style={{ minWidth: 0 }}>
            <Action variant="secondary" onClick={() => setDeleting(false)}>
              取消
            </Action>
            <Action
              variant="danger"
              disabled={groupBusy}
              onClick={() => void removeGroup()}
            >
              删除分组及全部数据
            </Action>
          </HStack>
        </Modal>
      )}
      {editing && (
        <DayEditor
          groupId={groupId}
          kind={editing.kind}
          row={editing.row}
          data={data}
          day={day}
          onClose={() => setEditing(null)}
          onSave={finish}
        />
      )}
      {comparing && (
        <CompatibilityDialog
          ids={selected}
          data={data}
          day={day}
          onClose={() => setComparing(false)}
        />
      )}
    </VStack>
  );
}
type Compatibility = {
  compatible: boolean;
  reasons: string[];
  groups: {
    warehouse_name: string;
    store_ids: string[];
    parameters: QuoteVersion;
  }[];
  notice: string;
};
const errorText = (e: unknown) =>
  e instanceof Error ? e.message : "操作未完成，请重试。";
function DayEditor({
  kind,
  row,
  data,
  day,
  groupId,
  onClose,
  onSave,
}: {
  groupId?: string;
  kind: "warehouses" | "vehicles";
  row: Row;
  data: Catalog;
  day: string;
  onClose: () => void;
  onSave: () => void;
}) {
  const [departure, setDeparture] = useState(
    String(row.day_departure_time ?? "08:00"),
  );
  const [warehouse, setWarehouse] = useState(
    String(row.day_warehouse_id ?? ""),
  );
  const [available, setAvailable] = useState(String(row.day_available));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api(
        groupId
          ? "/planning/groups/" + groupId + "/" + kind + "/" + row.id
          : "/" + kind + "/" + row.id + "/days/" + day,
        "PUT",
        {
          expected_revision: row.day_revision,
          ...(kind === "warehouses"
            ? { departure_time: departure }
            : { warehouse_id: warehouse, available: available === "true" }),
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
      title="当日安排"
      description={`${day} · ${row.name ?? row.plate} · 北京时间`}
      onClose={onClose}
      busy={busy}
    >
      <VStack as="form" gap={4} onSubmit={save}>
        {kind === "warehouses" ? (
          <>
            <label htmlFor="day-departure">
              <Text type="label" display="block">
                当天最早发车时间
              </Text>
            </label>
            <Input
              id="day-departure"
              type="time"
              required
              value={departure}
              onInput={(e) => setDeparture(e.currentTarget.value)}
              onChange={(e) => setDeparture(e.target.value)}
            />
          </>
        ) : (
          <>
            <label htmlFor="day-warehouse">
              <Text type="label" display="block">
                当天出货仓
              </Text>
            </label>
            <Select
              id="day-warehouse"
              value={warehouse}
              required
              onChange={(e) => setWarehouse(e.target.value)}
            >
              {data.warehouses.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.code} · {w.name}
                </option>
              ))}
            </Select>
            <label htmlFor="day-available">
              <Text type="label" display="block">
                当天是否可用
              </Text>
            </label>
            <Select
              id="day-available"
              value={available}
              onChange={(e) => setAvailable(e.target.value)}
            >
              <option value="true">可用</option>
              <option value="false">不可用</option>
            </Select>
          </>
        )}
        <Text type="supporting" as="p">
          仅影响所选日期；默认资料和其他日期的单独安排保持原值。
        </Text>
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
            {busy && <Spinner />} 保存当日安排
          </Action>
        </HStack>
      </VStack>
    </Modal>
  );
}
function CompatibilityDialog({
  ids,
  data,
  day,
  onClose,
}: {
  ids: string[];
  data: Catalog;
  day: string;
  onClose: () => void;
}) {
  const [result, setResult] = useState<Compatibility | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Compatibility>("/master-data/compatibility", "POST", {
      store_ids: ids,
      delivery_date: day,
    })
      .then(setResult)
      .catch((e) => setError(errorText(e)));
  }, []);
  return (
    <Modal
      wide
      title="报价兼容判断"
      description={`${day} · 已选择 ${ids.length} 家门店`}
      onClose={onClose}
    >
      <ErrorMessage text={error} />
      {result ? (
        <>
          <VStack gap={3} style={{ minWidth: 0 }}>
            <GitCompareArrows size={19} />
            {result.compatible
              ? "同仓且完整报价参数一致，报价兼容"
              : "所选门店不能合为同一报价线路"}
          </VStack>
          {result.reasons.map((r, i) => (
            <Text type="supporting" as="p" key={i}>
              {r}
            </Text>
          ))}
          <VStack gap={3} style={{ minWidth: 0 }}>
            {result.groups.map((g, i) => (
              <VStack gap={3} style={{ minWidth: 0 }} key={i}>
                <VStack gap={3} style={{ minWidth: 0 }}>
                  <Text type="label" weight="semibold" hasTabularNumbers>
                    {g.warehouse_name}
                  </Text>
                  <Text type="body" color="accent">
                    兼容组 {i + 1}
                  </Text>
                </VStack>
                <Text type="body" as="p">
                  {quoteDescription(g.parameters)}
                </Text>
                <Text type="supporting" display="block">
                  {g.store_ids
                    .map((id) => data.stores.find((s) => s.id === id)?.name)
                    .join("、")}
                </Text>
              </VStack>
            ))}
          </VStack>
          <Text type="supporting" as="p">
            {result.notice}
          </Text>
        </>
      ) : (
        !error && (
          <VStack
            gap={3}
            paddingBlock={6}
            align="center"
            style={{ minWidth: 0, textAlign: "center" }}
          >
            <Spinner /> 正在比较完整参数
          </VStack>
        )
      )}
    </Modal>
  );
}
