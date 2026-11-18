import { useState } from "react";
import type { ResourceStatistics } from "../types/statistics";
type DeviceRef = { cluster: string; node: string; device_uuid: string };
const names: Record<string, string> = { yz: "亦庄", zw: "兆维" };
export function BusinessOwnership({
  data,
  allocation,
  onDevices,
}: {
  data: ResourceStatistics;
  allocation: (v: number | null) => string;
  onDevices: (devices: DeviceRef[], title: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const allModels = data.business_models.filter((m) => m.model_key !== null);
  const categories = [
    ...new Set(allModels.map((m) => m.category || "未分类")),
  ].sort();
  const matches = (text: string) =>
    text.toLowerCase().includes(query.trim().toLowerCase());
  const models = allModels.filter(
    (m) =>
      (category === "all" || category === (m.category || "未分类")) &&
      matches(
        [
          m.name,
          m.model_key,
          m.category,
          m.description,
          ...Object.entries(m.attributes ?? {}).flat(),
          ...(m.model_names ?? []),
          ...(m.workloads ?? []).map(
            (w) => `${names[w.cluster]} ${w.namespace} ${w.pod}`,
          ),
        ].join(" "),
      ),
  );
  const unknown = data.business_models.find((m) => m.model_key === null);
  const workloads = (data.unmapped_workloads ?? []).filter((w) =>
    matches(`${names[w.cluster]} ${w.namespace} ${w.pod}`),
  );
  const count = data.unmapped_workload_count ?? data.unmapped_workloads?.length;
  const button = (
    devices: DeviceRef[] | undefined,
    truncated: boolean | undefined,
    title: string,
  ) => (
    <button
      type="button"
      className="link"
      disabled={!devices?.length}
      onClick={() => onDevices(devices ?? [], title)}
    >
      查看关联 GPU{truncated ? "（前100张）" : ""}
    </button>
  );
  return (
    <div className="ownership-content">
      <div className="ownership-summary">
        <span>
          已映射模型 <strong>{allModels.length}</strong>
        </span>
        <span>
          归属类别 <strong>{categories.length}</strong>
        </span>
        <span>
          未映射负载 <strong>{count ?? "—"}</strong>
        </span>
      </div>
      <div className="asset-filters ownership-filters">
        <label>
          归属类别
          <select
            aria-label="归属类别"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="all">全部类别</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
            <option value="unmapped">未映射</option>
          </select>
        </label>
        <label className="search">
          搜索模型或 Pod
          <input
            aria-label="搜索业务模型"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="名称、模型版本、Namespace / Pod"
          />
        </label>
        {(query || category !== "all") && (
          <button
            className="link"
            onClick={() => {
              setQuery("");
              setCategory("all");
            }}
          >
            清除筛选
          </button>
        )}
      </div>
      {category !== "unmapped" && (
        <section aria-label="已映射业务模型">
          <h3>已映射模型 · {models.length}</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>业务模型</th>
                  <th>归属类别</th>
                  <th>已观测分配额度</th>
                  <th>关联 GPU</th>
                  <th>关联机器</th>
                  <th>容器数</th>
                  <th>跨归属组共享卡</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.model_key}>
                    <td>
                      <strong>{m.name}</strong>
                      <details className="model-description">
                        <summary>查看模型与 Pod</summary>
                        {m.model_names?.length ? (
                          <p>{m.model_names.join("；")}</p>
                        ) : null}
                        {m.description && <p>{m.description}</p>}
                        {Object.entries(m.attributes ?? {}).map(
                          ([label, value]) => (
                            <p key={label}>
                              {label}：{value}
                            </p>
                          ),
                        )}
                        {(m.workloads ?? []).map((w) => (
                          <p key={JSON.stringify(w)}>
                            {names[w.cluster]} · {w.namespace} / {w.pod}
                          </p>
                        ))}
                        {!m.workloads?.length && <p>{m.model_key}</p>}
                        {m.workloads_truncated && <p>仅显示前100个关联 Pod</p>}
                      </details>
                    </td>
                    <td>
                      <span className="ownership-category">
                        {m.category || "未分类"}
                      </span>
                    </td>
                    <td>{allocation(m.allocated_raw)}</td>
                    <td>{m.gpu_count}</td>
                    <td>{m.machine_count}</td>
                    <td>{m.container_count}</td>
                    <td>{m.shared_gpu_count}</td>
                    <td>
                      {button(
                        m.associated_devices,
                        m.devices_truncated,
                        m.name,
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {models.length === 0 && <p className="empty">没有匹配的业务模型</p>}
        </section>
      )}
      {(category === "all" || category === "unmapped") && (
        <section aria-label="未映射业务负载">
          <div className="ownership-heading">
            <h3>未映射负载</h3>
          </div>
          {data.unmapped_workloads ? (
            <>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>集群 / Namespace</th>
                      <th>Pod</th>
                      <th>模型归属</th>
                      <th>已观测分配额度</th>
                      <th>关联 GPU / 机器</th>
                      <th>容器数</th>
                      <th>详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {workloads.map((w) => (
                      <tr key={JSON.stringify([w.cluster, w.namespace, w.pod])}>
                        <td>
                          {names[w.cluster]}
                          <small>{w.namespace}</small>
                        </td>
                        <td>{w.pod}</td>
                        <td>
                          <span className="ownership-pending">未映射</span>
                        </td>
                        <td>{allocation(w.allocated_raw)}</td>
                        <td>
                          {w.gpu_count} / {w.machine_count}
                        </td>
                        <td>{w.container_count}</td>
                        <td>
                          {button(
                            w.associated_devices,
                            w.devices_truncated,
                            `${names[w.cluster]} · ${w.namespace}/${w.pod}`,
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {workloads.length === 0 && (
                <p className="empty">
                  {query ? "没有匹配的负载" : "暂无未映射负载"}
                </p>
              )}
              {data.unmapped_workloads_truncated && (
                <p className="notice">
                  共有 {count} 个未映射负载，此处展示前{" "}
                  {data.unmapped_workloads.length} 个。
                </p>
              )}
            </>
          ) : (
            <p className="notice">
              当前响应尚无未映射负载明细，请更新后端。
              {unknown
                ? `未映射分配额度：${allocation(unknown.allocated_raw)}。`
                : ""}
            </p>
          )}
        </section>
      )}
      {allModels.length === 0 && (
        <p className="trend-note">
          暂无已匹配的 GPU 分配记录；未映射项保留真实 Namespace / Pod。
        </p>
      )}
    </div>
  );
}
