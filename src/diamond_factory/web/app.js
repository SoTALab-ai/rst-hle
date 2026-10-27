const $ = (selector) => document.querySelector(selector);
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on"))
      node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "className") node.className = value;
    else if (value != null && value !== false)
      node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child != null && child !== false)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  }
  return node;
}
const paths = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  layers: "m12 3 10 5-10 5L2 8z M2 12l10 5 10-5 M2 16l10 5 10-5",
  list: "M9 5h12 M9 12h12 M9 19h12 M3 5h1 M3 12h1 M3 19h1",
  book: "M12 5v16 M12 5C8 2 4 3 2 4v15c4-2 7-1 10 2 3-3 6-4 10-2V4c-4-2-7-1-10 1",
  shield: "m12 3 9 4v6c0 5-9 9-9 9S3 18 3 13V7z m-4 9 3 3 5-6",
  money: "M12 2v20 M17 5H9a4 4 0 0 0 0 8h6a3 3 0 0 1 0 6H6",
  archive: "M3 3h18v5H3z M5 8v13h14V8 M10 12h4",
  refresh: "M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 14 6 M4 12a8 8 0 0 0 14 6",
  search: "M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  arrow: "M5 12h14 m-5-5 5 5-5 5",
  back: "M19 12H5 m5-5-5 5 5 5",
  download: "M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5",
  chevron: "m14 6-6 6 6 6",
  check: "m5 12 4 4L19 6",
  copy: "M9 9h12v12H9z M15 5V3H3v12h2",
  clock: "M12 7v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  dot: "M12 3v18 M3 12h18",
};
function icon(name) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.6",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    node.setAttribute(k, v);
  const p = document.createElementNS(node.namespaceURI, "path");
  p.setAttribute("d", paths[name] || paths.grid);
  node.append(p);
  return node;
}
const labels = {
  accepted: ["已留存", "green"],
  screened: ["初筛通过", "blue"],
  quality_seed: ["质检种子", "amber"],
  rejected: ["已拒绝", "red"],
  running: ["运行中", "blue"],
  completed: ["已完成", "green"],
  completed_with_errors: ["完成 · 有阻断", "amber"],
  failed: ["运行失败", "red"],
  stopped: ["已停止", "gray"],
  unknown: ["未记录", "gray"],
  Math: ["数学", "blue"],
  Physics: ["物理", "purple"],
  pilot: ["试产", "blue"],
  production: ["生产", "green"],
};
const roleLabels = {
  generator: "作者",
  auditor: "综合审查",
  consistency: "科学质检",
  solver: "GLM 解题",
  judge: "答案判分",
};
const routes = [
  ["overview", "合成产线", "grid"],
  ["questions", "题目队列", "list"],
  ["runs", "运行批次", "layers"],
  ["papers", "论文与来源", "book"],
  ["quality", "质量审查", "shield"],
  ["archives", "实验档案", "archive"],
  ["costs", "成本收据", "money"],
];
const state = {
  data: null,
  error: "",
  loading: false,
  filters: {},
  detail: null,
  detailKey: "",
  detailError: "",
  detailLoading: false,
};
const openSamples = new Set();
let collapsed = false;
try {
  collapsed = localStorage.getItem("rst.sidebar.collapsed") === "true";
} catch {
  /* 受限存储仍可浏览。 */
}
let requestId = 0;
const fmt = (value) =>
  value == null ? "—" : Number(value).toLocaleString("zh-CN");
const usd = (value) =>
  value == null
    ? "未核算"
    : "$" +
      Number(value).toLocaleString("en-US", {
        minimumFractionDigits: 4,
        maximumFractionDigits: 6,
      });
const date = (value) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "未记录";
const chip = (value) =>
  el(
    "span",
    { className: `pill ${labels[value]?.[1] || "gray"}` },
    labels[value]?.[0] || value || "未记录",
  );
const link = (text, href, cls = "link") =>
  el(
    "a",
    {
      href,
      className: cls,
      "aria-current": cls.includes("active") ? "page" : null,
    },
    text,
  );
const button = (text, name, action, cls = "button") =>
  el(
    "button",
    {
      type: "button",
      className: cls,
      onclick: action,
      "aria-label": text || (name === "refresh" ? "刷新数据" : name),
    },
    name && icon(name),
    text,
  );
const empty = (text = "没有符合条件的记录", hint = "调整筛选条件后重试。") =>
  el(
    "div",
    { className: "empty" },
    icon("layers"),
    el("strong", {}, text),
    el("p", {}, hint),
  );
const panel = (title, extra, ...content) =>
  el(
    "section",
    { className: "panel" },
    el("div", { className: "panel-heading" }, el("h2", {}, title), extra),
    ...content,
  );
const metric = (label, value, hint, name, accent = "") =>
  el(
    "section",
    { className: `metric ${accent}` },
    el("div", { className: "metric-label" }, label, icon(name)),
    el("strong", {}, value),
    el("small", {}, hint),
  );
const jsonView = (data) =>
  el("pre", { className: "json" }, JSON.stringify(data, null, 2));
const pathFor = (kind, id) => `#${kind}/${encodeURIComponent(id)}`;
function current() {
  const [path, query = ""] = location.hash.slice(1).split("?");
  const [page = "overview", rawId] = path.split("/");
  let id;
  try {
    id = rawId ? decodeURIComponent(rawId) : null;
  } catch {
    id = rawId;
  }
  return { page: page || "overview", id, query: new URLSearchParams(query) };
}
function fields(items) {
  return el(
    "dl",
    { className: "fields" },
    items.map(([key, value]) =>
      el("div", {}, el("dt", {}, key), el("dd", {}, value ?? "未记录")),
    ),
  );
}
function prose(text) {
  const node = el("div", { className: "prose" });
  // 只把明确的TeX定界符交给KaTeX；普通文本始终以textContent写入。
  const regex =
    /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<!\\)\$([^$\n]+?)\$/g;
  let start = 0;
  for (const m of String(text).matchAll(regex)) {
    node.append(document.createTextNode(text.slice(start, m.index)));
    const math = el("span", {
      className: m[1] || m[2] ? "math-block" : "math-inline",
    });
    if (window.katex) {
      try {
        window.katex.render(m[1] || m[2] || m[3] || m[4], math, {
          output: "mathml",
          displayMode: Boolean(m[1] || m[2]),
          throwOnError: false,
          trust: false,
          maxSize: 20,
          maxExpand: 1000,
        });
      } catch {
        math.textContent = m[0];
      }
    } else math.textContent = m[0];
    node.append(math);
    start = m.index + m[0].length;
  }
  node.append(document.createTextNode(String(text).slice(start)));
  return node;
}
function download(rows, name) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" }),
  );
  const a = el("a", { href: url, download: `rst-hle-${name}.json` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function table(headers, rows, hint) {
  if (!rows.length) return empty(hint);
  return el(
    "div",
    { className: "table-wrap" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          headers.map((h) => el("th", { scope: "col" }, h)),
        ),
      ),
      el(
        "tbody",
        {},
        rows.map((cells) =>
          el(
            "tr",
            {},
            cells.map((cell) => el("td", {}, cell)),
          ),
        ),
      ),
    ),
  );
}
function statusCount(name) {
  return state.data.statuses[name] || 0;
}
function sidebar() {
  return el(
    "aside",
    {},
    el(
      "div",
      { className: "brand" },
      el("span", { className: "brand-mark" }, "R"),
      el(
        "div",
        { className: "brand-copy" },
        el("b", {}, "RST-HLE"),
        el("small", {}, "QUESTION FACTORY"),
      ),
      el(
        "button",
        {
          className: "collapse-button",
          type: "button",
          "aria-label": collapsed ? "展开侧栏" : "收起侧栏",
          onclick: () => {
            collapsed = !collapsed;
            try {
              localStorage.setItem("rst.sidebar.collapsed", String(collapsed));
            } catch {}
            render();
          },
        },
        icon("chevron"),
      ),
    ),
    el(
      "nav",
      { "aria-label": "主导航" },
      el("p", { className: "nav-group" }, "工作空间"),
      routes.map(([id, title, name], i) => [
        i === 5 && el("p", { className: "nav-group" }, "研究与追踪"),
        link(
          [
            icon(name),
            el("span", { className: "nav-text" }, title),
            id === "questions" &&
              state.data &&
              el(
                "span",
                { className: "nav-count" },
                fmt(state.data.questions.length),
              ),
          ],
          `#${id}`,
          current().page === id ||
            (id === "questions" && current().page === "question") ||
            (id === "runs" && current().page === "run") ||
            (id === "archives" && current().page === "archive")
            ? "nav-link active"
            : "nav-link",
        ),
      ]),
    ),
    el(
      "footer",
      {},
      el("div", { className: "workspace-avatar" }, "研"),
      el(
        "div",
        { className: "nav-text" },
        el("strong", {}, "理论出题工作台"),
        el("small", {}, "个人环境 · 只读观察"),
      ),
    ),
  );
}
function topbar() {
  return el(
    "header",
    { className: "topbar" },
    el(
      "div",
      { className: "breadcrumbs" },
      el("span", {}, "工作空间"),
      el("span", {}, "/"),
      el(
        "strong",
        {},
        routes.find((r) => r[0] === current().page)?.[1] || "记录详情",
      ),
    ),
    el(
      "div",
      { className: "top-actions" },
      el(
        "span",
        { className: "connection" },
        el("i", { className: state.error ? "offline" : "" }),
        state.error
          ? "连接异常"
          : state.loading
            ? "同步中"
            : state.data
              ? `${state.data.backend === "postgresql" ? "PostgreSQL" : "SQLite"} 已连接`
              : "正在连接",
      ),
      button("", "refresh", () => refresh(), "icon-button"),
      el("span", { className: "readonly" }, "只读"),
    ),
  );
}
function heading(title, description, action) {
  return el(
    "div",
    { className: "page-heading" },
    el(
      "div",
      {},
      el("div", { className: "eyebrow" }, "RST-HLE WORKSPACE"),
      el("h1", {}, title),
      el("p", {}, description),
    ),
    action,
  );
}
function overview() {
  const d = state.data,
    samples = d.questions.reduce((n, q) => n + q.samples, 0);
  const gates = [
    ["形成题目", d.questions.length],
    ["自洽凭证有效", d.questions.filter((q) => q.consistency_passed).length],
    [
      "Verifier 凭证有效",
      d.questions.filter((q) => q.consistency_passed && q.verifier_passed)
        .length,
    ],
    ["完整难度记录", d.questions.filter((q) => q.difficulty_valid).length],
    ["生产留存", statusCount("accepted")],
  ];
  return [
    heading(
      "合成产线",
      "从论文启发，到可追溯的独立题目。",
      button(
        "查看题目队列",
        "arrow",
        () => (location.hash = "questions"),
        "button primary",
      ),
    ),
    el(
      "div",
      { className: "metrics" },
      metric(
        "当前数据库批次",
        fmt(d.runs.length),
        "当前连接范围，不含历史档案",
        "layers",
      ),
      metric(
        "已形成题目版本",
        fmt(d.questions.length),
        "包含拒绝、种子和所有代次",
        "list",
      ),
      metric("已记录难度样本", fmt(samples), "答卷与墙钟超时分别保留", "clock"),
      metric(
        "生产留存",
        fmt(statusCount("accepted")),
        "仅统计 accepted 状态",
        "shield",
        "accent",
      ),
    ),
    el(
      "div",
      { className: "section-label" },
      el("h2", {}, "质量与进度"),
      el("span", {}, "当前数据库快照"),
    ),
    el(
      "div",
      { className: "overview-grid" },
      panel(
        "阶段覆盖",
        el("span", { className: "muted" }, "当前有效凭证"),
        el(
          "div",
          { className: "gate-list" },
          gates.map(([name, count], i) =>
            el(
              "div",
              { className: "gate-row" },
              el(
                "span",
                { className: "step-index" },
                String(i + 1).padStart(2, "0"),
              ),
              el("span", {}, name),
              el("progress", {
                max: Math.max(d.questions.length, 1),
                value: count,
                "aria-label": name,
              }),
              el("strong", {}, fmt(count)),
            ),
          ),
        ),
        el(
          "p",
          { className: "panel-note" },
          "各阶段展示当前版本凭证覆盖；不代表历史累计吞吐。初筛通过与生产留存分开统计。",
        ),
      ),
      panel(
        "题目状态",
        link("查看队列 →", "#questions"),
        el(
          "div",
          { className: "status-grid" },
          ["quality_seed", "rejected", "screened", "accepted"].map((status) =>
            el(
              "a",
              { href: `#questions?status=${status}`, className: "status-card" },
              chip(status),
              el("strong", {}, fmt(statusCount(status))),
            ),
          ),
        ),
        el(
          "p",
          { className: "panel-note" },
          "科学审查尚未完成或样本不足的记录，不计为有效难度通过。",
        ),
      ),
    ),
    panel(
      "运行批次",
      link("全部批次 →", "#runs"),
      runTable(d.runs.slice(0, 6)),
    ),
    d.archives.length > 0 &&
      panel(
        "最近实验档案",
        link("浏览档案 →", "#archives"),
        archiveTable(d.archives.slice(0, 3)),
      ),
    el(
      "div",
      { className: "info-strip" },
      icon("shield"),
      el(
        "p",
        {},
        "当前页面只读连接选定数据库。历史报告在实验档案单独展示；缺失费用保留为未知。",
      ),
    ),
  ];
}
function filterBar(page, categories, statuses) {
  const f = (state.filters[page] ||= {});
  function set(key, value) {
    f[key] = value;
    f.page = 1;
    render();
  }
  const select = (id, label, values) =>
    el(
      "label",
      { className: "filter-field" },
      el("span", { className: "sr-only" }, label),
      el(
        "select",
        { id, value: f[id] || "", onchange: (e) => set(id, e.target.value) },
        el("option", { value: "" }, label),
        values.map(([value, text]) =>
          el("option", { value, selected: f[id] === value }, text),
        ),
      ),
    );
  return el(
    "div",
    { className: "filters" },
    el(
      "label",
      { className: "search-field" },
      icon("search"),
      el("input", {
        id: `search-${page}`,
        type: "search",
        value: f.search || "",
        placeholder:
          page === "papers" ? "搜索标题、DOI 或来源 ID" : "搜索 ID、题面或子类",
        oninput: (e) => set("search", e.target.value),
        "aria-label": "搜索记录",
      }),
    ),
    categories &&
      select(
        "category",
        "全部学科",
        categories.map((x) => [x, labels[x]?.[0] || x]),
      ),
    statuses &&
      select(
        "status",
        "全部状态",
        statuses.map((x) => [x, labels[x]?.[0] || x]),
      ),
    (f.search || f.category || f.status) &&
      button(
        "重置",
        null,
        () => {
          state.filters[page] = {};
          render();
        },
        "button quiet",
      ),
  );
}
function paged(page, rows, headers, cells) {
  const f = (state.filters[page] ||= {}),
    total = rows.length;
  const max = Math.max(1, Math.ceil(total / 20)),
    index = Math.min(f.page || 1, max);
  return [
    table(headers, rows.slice((index - 1) * 20, index * 20).map(cells)),
    el(
      "div",
      { className: "pagination" },
      el("span", {}, `共 ${fmt(total)} 条 · 每页 20 条`),
      el(
        "div",
        {},
        el(
          "button",
          {
            className: "button",
            disabled: index <= 1,
            onclick: () => {
              f.page = index - 1;
              render();
            },
          },
          "上一页",
        ),
        el("span", {}, `${index} / ${max}`),
        el(
          "button",
          {
            className: "button",
            disabled: index >= max,
            onclick: () => {
              f.page = index + 1;
              render();
            },
          },
          "下一页",
        ),
      ),
    ),
  ];
}
function questionRows(page = "questions") {
  const f = (state.filters[page] ||= {}),
    query = current().query,
    run = query.get("run");
  return state.data.questions.filter(
    (q) =>
      (!run || q.run_id === run) &&
      (!f.category || q.category === f.category) &&
      (!f.status || q.status === f.status) &&
      (!f.search ||
        `${q.id} ${q.question} ${q.subtopic}`
          .toLowerCase()
          .includes(f.search.toLowerCase())),
  );
}
function sampleCell(q) {
  return el(
    "div",
    { className: "sample-cell" },
    el(
      "strong",
      {},
      q.sample_error
        ? "记录异常"
        : q.samples
          ? `${q.correct ?? "—"} / ${q.samples}`
          : "尚未采样",
    ),
    el("small", {}, `${q.answered} 答卷 · ${q.deadlines} 超时`),
  );
}
function questionCells(q) {
  return [
    el(
      "div",
      { className: "identity" },
      link(q.id, pathFor("question", q.id), "mono link"),
      el("span", { className: "excerpt" }, q.question),
    ),
    el(
      "div",
      {},
      chip(q.category),
      el("small", { className: "cell-subtitle" }, q.subtopic),
    ),
    el("span", { className: "mono" }, `R${q.generation}`),
    chip(q.status),
    sampleCell(q),
    link("查看详情 →", pathFor("question", q.id)),
  ];
}
function questions() {
  const rows = questionRows(),
    run = current().query.get("run");
  return [
    heading(
      "题目队列",
      run
        ? `批次 ${run} 的全部候选版本。`
        : "筛选候选版本，查看推导、样本与拒绝原因。",
      button("导出筛选摘要", "download", () => download(rows, "questions")),
    ),
    current().query.size > 0 &&
      el(
        "div",
        { className: "notice" },
        "已从关联页面设置筛选。",
        link("清除关联筛选", "#questions"),
      ),
    panel(
      "题目列表",
      el("span", { className: "count-badge" }, fmt(rows.length)),
      filterBar(
        "questions",
        ["Math", "Physics"],
        ["quality_seed", "rejected", "screened", "accepted"],
      ),
      paged(
        "questions",
        rows,
        ["题目 / ID", "学科与子类", "代次", "状态", "正确 / 已记录", "操作"],
        questionCells,
      ),
    ),
  ];
}
function runTable(rows) {
  return table(
    ["运行身份", "模式", "状态", "形成版本", "生产留存", "HTTP 预约"],
    rows.map((r) => [
      el(
        "div",
        { className: "identity" },
        link(r.id, pathFor("run", r.id), "link mono"),
        el("small", {}, r.updated_at ? date(r.updated_at) : "更新时间未记录"),
      ),
      chip(r.mode),
      chip(r.status),
      fmt(r.candidates),
      fmt(r.accepted),
      fmt(r.http_attempts),
    ]),
    "暂无运行批次",
  );
}
function runs() {
  const f = (state.filters.runs ||= {}),
    rows = state.data.runs.filter(
      (r) =>
        !f.search ||
        `${r.id} ${r.status}`.toLowerCase().includes(f.search.toLowerCase()),
    );
  return [
    heading(
      "运行批次",
      "查看运行身份、冻结指纹和关联题目。",
      button("导出批次摘要", "download", () => download(rows, "runs")),
    ),
    panel(
      "当前数据库批次",
      el("span", { className: "count-badge" }, fmt(rows.length)),
      filterBar("runs"),
      runTable(rows),
    ),
  ];
}
function papers() {
  const f = (state.filters.papers ||= {}),
    rows = state.data.papers.filter(
      (p) =>
        (!f.category || p.domain === f.category) &&
        (!f.search ||
          `${p.title} ${p.id} ${p.doi || ""} ${p.journal}`
            .toLowerCase()
            .includes(f.search.toLowerCase())),
    );
  return [
    heading(
      "论文与来源",
      "当前数据库登记的完整材料索引与题源；登记不等于科学审查通过。",
      button("导出材料索引", "download", () => download(rows, "papers")),
    ),
    panel(
      "材料索引",
      el("span", { className: "count-badge" }, fmt(rows.length)),
      filterBar("papers", ["Math", "Physics"]),
      paged(
        "papers",
        rows,
        ["论文 / 来源", "学科", "期刊 / 类型", "文件与页数", "身份与引用"],
        (p) => [
          el(
            "div",
            { className: "identity" },
            el("strong", {}, p.title),
            el("span", { className: "mono cell-subtitle" }, p.id),
          ),
          chip(p.domain || "未标注"),
          el(
            "div",
            {},
            p.journal || "—",
            el("small", { className: "cell-subtitle" }, p.kind),
          ),
          p.documents == null
            ? "未记录"
            : `${p.documents} 文件 / ${p.pages} 页`,
          el(
            "div",
            { className: "source-identity" },
            p.doi &&
              link(p.doi, `https://doi.org/${encodeURIComponent(p.doi)}`),
            p.sha256 &&
              el(
                "span",
                { className: "mono", title: p.sha256 },
                `SHA256 ${p.sha256.slice(0, 12)}…`,
              ),
          ),
        ],
      ),
    ),
    el(
      "p",
      { className: "footnote" },
      "文件数和物理页数仅说明已登记对象；不据此推断完整解析、来源一致性或科学正确性。论文库使用独立数据库时，需在该环境启动控制台。",
    ),
  ];
}
function quality() {
  const f = (state.filters.quality ||= {}),
    rows = questionRows("quality");
  const gates = [
    ["综合审查", "audit"],
    ["自洽与反代", "consistency"],
    ["Verifier 对抗", "verifier_assessment"],
    ["最难步骤", "bottleneck_assessment"],
    ["RST 净负担", "rst_origin"],
  ];
  return [
    heading("质量审查", "从具体版本查看门禁证据、失败原因与覆盖边界。"),
    el(
      "div",
      { className: "metrics three" },
      metric(
        "自洽凭证有效",
        fmt(state.data.questions.filter((q) => q.consistency_passed).length),
        "按当前候选与来源校验",
        "shield",
      ),
      metric(
        "Verifier 凭证有效",
        fmt(state.data.questions.filter((q) => q.verifier_passed).length),
        "审批绑定当前版本",
        "check",
      ),
      metric(
        "样本不完整或异常",
        fmt(
          state.data.questions.filter(
            (q) => q.samples > 0 && (q.samples !== 16 || q.sample_error),
          ).length,
        ),
        "不计为完整难度结果",
        "clock",
      ),
    ),
    panel(
      "审查证据索引",
      el("span", { className: "muted" }, "逐题检查"),
      filterBar(
        "quality",
        ["Math", "Physics"],
        ["quality_seed", "rejected", "screened", "accepted"],
      ),
      paged(
        "quality",
        rows,
        ["题目身份", "自洽凭证", "Verifier 凭证", "难度记录", "当前原因"],
        (q) => [
          link(q.id, pathFor("question", q.id) + "?tab=quality", "mono link"),
          badge(q.consistency_passed),
          badge(q.verifier_passed),
          q.difficulty_valid
            ? chip("完整且有效")
            : chip(
                q.sample_error
                  ? "样本异常"
                  : q.samples
                    ? "未完成 / 未认证"
                    : "未测",
              ),
          el(
            "div",
            { className: "reason" },
            q.reasons.join("；") || "无拒绝原因记录",
          ),
        ],
      ),
    ),
    el(
      "p",
      { className: "footnote" },
      `详情提供${gates.map((g) => g[0]).join("、")}的原始结构化证据。有限点反代不能证明全域；模型审查不能代替形式化证明。`,
    ),
  ];
}
function badge(ok) {
  return el(
    "span",
    { className: `pill ${ok ? "green" : "gray"}` },
    ok ? "凭证有效" : "未认证",
  );
}
function archiveTable(rows) {
  return table(
    ["历史运行身份", "终态", "计划线数", "形成版本", "交付留存", "已知费用"],
    rows.map((a) => [
      el(
        "div",
        { className: "identity" },
        link(a.id, pathFor("archive", a.id), "mono link"),
        el("small", {}, date(a.finalized_at)),
      ),
      chip(a.status),
      fmt(a.planned),
      fmt(a.formed),
      fmt(a.retained),
      usd(a.known_cost_usd),
    ]),
    "暂无实验档案",
  );
}
function archives() {
  const f = (state.filters.archives ||= {}),
    rows = state.data.archives.filter(
      (a) => !f.search || a.id.toLowerCase().includes(f.search.toLowerCase()),
    );
  return [
    heading(
      "实验档案",
      "已归档的脱敏报告独立展示，保留原批次结论。",
      button("导出档案索引", "download", () => download(rows, "archives")),
    ),
    el(
      "div",
      { className: "notice" },
      "历史报告来自启动时指定的目录；不合并进当前数据库的题目数量、有效凭证或实时状态。",
    ),
    panel(
      "历史实验报告",
      el("span", { className: "count-badge" }, fmt(rows.length)),
      filterBar("archives"),
      archiveTable(rows),
    ),
  ];
}
function costs() {
  const archives = state.data.archives;
  return [
    heading("成本收据", "按原支付批次查看已结算收据；未知用量与在途分别展示。"),
    el(
      "div",
      { className: "notice" },
      "此页展示档案内 cost 的原批次核算；不重复累加累计账本，也不从成功响应数推算全部 HTTP 费用。",
    ),
    archives.length
      ? archives.map((a) =>
          panel(
            a.id,
            link("查看报告 →", pathFor("archive", a.id)),
            el(
              "div",
              { className: "cost-summary" },
              el(
                "div",
                {},
                el("span", {}, "已知 API 费用"),
                el("strong", {}, usd(a.known_cost_usd)),
              ),
              fields([
                ["HTTP 收据", fmt(a.http_attempts)],
                ["用量未知", fmt(a.unknown_usage)],
                ["在途 / 未结算", fmt(a.unsettled)],
                ["价格快照", a.pricing_date || "未记录"],
              ]),
            ),
            table(
              ["角色", "HTTP 尝试", "已知费用", "用量未知"],
              Object.entries(a.by_role).map(([role, value]) => [
                roleLabels[role] || role,
                fmt(value.http_attempts),
                usd(value.known_cost_usd),
                fmt(value.unknown_usage_attempts),
              ]),
            ),
            el(
              "p",
              { className: "panel-note" },
              "原支付 run 归属；未包含 VM、磁盘与网络账单。缓存复用不重新计入模型采购。",
            ),
          ),
        )
      : empty(
          "尚无成本档案",
          "启动时通过 --reports 指定已有脱敏实验报告目录。",
        ),
  ];
}
async function getDetail(kind, id, force = false) {
  const key = kind + "/" + id;
  if (state.detailKey === key && (!force || state.detailLoading)) return;
  const preserve = state.detailKey === key && state.detail;
  state.detailKey = key;
  state.detailError = "";
  if (!preserve) {
    state.detail = null;
    state.detailLoading = true;
  }
  try {
    const response = await fetch(`/api/${kind}/${encodeURIComponent(id)}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(20000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "读取失败");
    if (state.detailKey === key) state.detail = data;
  } catch (error) {
    if (state.detailKey === key) state.detailError = error.message;
  } finally {
    if (state.detailKey === key) {
      state.detailLoading = false;
      render();
    }
  }
}
function detailPage(kind, id) {
  getDetail(kind, id);
  if (state.detailLoading)
    return empty("正在读取记录…", "校验对象与当前版本。");
  if (state.detailError)
    return [
      button("返回列表", "back", () => history.back()),
      el(
        "div",
        { className: "error", role: "alert" },
        state.detailError,
        button("重试", "refresh", () => {
          state.detailKey = "";
          render();
        }),
      ),
    ];
  if (!state.detail) return empty("没有可展示的记录");
  if (kind === "questions") return questionDetail(state.detail);
  if (kind === "runs") return runDetail(state.detail);
  return archiveDetail(state.detail);
}
function tabs(items) {
  const currentTab = current().query.get("tab") || items[0][0];
  return el(
    "nav",
    { className: "tabs", "aria-label": "详情分栏" },
    items.map(([id, text]) =>
      link(
        text,
        location.hash.split("?")[0] + "?tab=" + id,
        currentTab === id ? "tab active" : "tab",
      ),
    ),
  );
}
function questionDetail(data) {
  const c = data.candidate,
    q = data.summary,
    tab = current().query.get("tab") || "question";
  const siblings = state.data.questions
    .filter((x) => x.root_id === q.root_id)
    .sort((a, b) => a.generation - b.generation);
  const sections = {
    question: () => [
      panel(
        "题面",
        button("复制题面", "copy", () => copyText(c.item.question)),
        prose(c.item.question),
      ),
      panel(
        "作答义务与前提",
        null,
        fields([
          ["必需输出", c.item.required_outputs.join("\n")],
          ["题设前提", c.item.assumptions.join("\n")],
        ]),
      ),
    ],
    golden: () => [
      panel(
        "Golden 答案",
        button("复制答案", "copy", () => copyText(c.item.answer)),
        prose(c.item.answer),
      ),
      panel(
        "完整参考推导",
        null,
        ...c.solution.derivation.map((step, i) =>
          el(
            "div",
            { className: "derivation" },
            el("span", { className: "step-index" }, i + 1),
            prose(step),
          ),
        ),
      ),
      panel("解题说明", null, prose(c.item.rationale)),
    ],
    quality: () => [
      q.reasons.length > 0 &&
        panel(
          "当前拒绝 / 阻断原因",
          chip(q.status),
          ...q.reasons.map((r) => el("p", { className: "reason" }, r)),
        ),
      ...[
        "audit",
        "blueprint_audit",
        "bottleneck_assessment",
        "consistency",
        "verifier_assessment",
        "difficulty_rounds",
        "rst_origin",
      ].map((key) =>
        panel(
          {
            audit: "综合审查",
            blueprint_audit: "完整推导复核",
            bottleneck_assessment: "最难步骤与挖空",
            consistency: "自洽审查与精确反代",
            verifier_assessment: "Verifier 对抗",
            difficulty_rounds: "难度对抗历史",
            rst_origin: "RST 父子增量与净负担",
          }[key],
          null,
          c[key] && (!Array.isArray(c[key]) || c[key].length)
            ? jsonView(c[key])
            : el(
                "p",
                { className: "muted" },
                "该版本无此项记录，不推断为通过。",
              ),
        ),
      ),
    ],
    samples: () => [
      panel(
        "难度样本",
        sampleCell(q),
        el(
          "p",
          { className: "panel-note" },
          "正确 / 已记录包含有凭证的 600 秒墙钟超时；样本不足 16 次时不显示完整难度通过率。",
        ),
        c.trials.length || c.deadline_outcomes.length
          ? el(
              "div",
              { className: "sample-list" },
              ...c.trials.map((t) =>
                el(
                  "details",
                  {
                    className: "sample-detail",
                    open: openSamples.has(`${c.id}/${t.sample_id}`),
                    ontoggle: (e) => {
                      const key = `${c.id}/${t.sample_id}`;
                      e.target.open
                        ? openSamples.add(key)
                        : openSamples.delete(key);
                    },
                  },
                  el(
                    "summary",
                    {},
                    el("span", {}, `样本 ${t.sample_id + 1}`),
                    el(
                      "span",
                      {
                        className: `pill ${t.judgment.correct === "yes" ? "green" : "red"}`,
                      },
                      t.judgment.correct === "yes" ? "正确" : "错误",
                    ),
                    el("span", { className: "muted" }, t.solver_actual_model),
                  ),
                  el(
                    "div",
                    { className: "sample-content" },
                    fields([
                      ["解题请求", t.solver_request],
                      ["判分请求", t.judge_request],
                      ["实际 judge", t.judge_actual_model],
                    ]),
                    el("h3", {}, "完整答卷"),
                    prose(t.response),
                    el("h3", {}, "判分结果"),
                    jsonView(t.judgment),
                  ),
                ),
              ),
              ...c.deadline_outcomes.map((t) =>
                el(
                  "div",
                  { className: "deadline" },
                  chip("墙钟超时 · 计错"),
                  fields([
                    ["样本", t.sample_id + 1],
                    [
                      "限时 / 实际秒数",
                      `${t.deadline_seconds} / ${t.elapsed_seconds}`,
                    ],
                    ["请求身份", t.solver_request],
                  ]),
                  el(
                    "p",
                    {},
                    "此项没有答卷和 judge 请求；保留原始 deadline 记录。",
                  ),
                ),
              ),
            )
          : empty("尚未记录难度样本", "未采样不等于答错。"),
      ),
    ],
    lineage: () => [
      panel(
        "版本谱系",
        null,
        fields([
          ["Root", q.root_id],
          [
            "Parent",
            q.parent_id
              ? link(q.parent_id, pathFor("question", q.parent_id))
              : "无 · 根版本",
          ],
          ["代次", `R${q.generation}`],
          ["运行批次", link(q.run_id, pathFor("run", q.run_id))],
          ["构造操作", c.operator],
          ["来源模式", c.source_fidelity],
        ]),
        el(
          "div",
          { className: "lineage" },
          siblings.map((s) =>
            link(
              [
                el("span", { className: "pill blue" }, `R${s.generation}`),
                el("span", { className: "mono" }, s.id),
                chip(s.status),
              ],
              pathFor("question", s.id),
              "lineage-item",
            ),
          ),
        ),
      ),
      ...data.sources.map((s) =>
        panel(
          s.title,
          null,
          fields([
            ["来源 ID", s.id],
            ["URI", s.uri],
            ["正文哈希", s.sha256],
            ["登记时间", date(s.retrieved_at)],
          ]),
        ),
      ),
      panel(
        "来源说明与证据",
        null,
        jsonView({ source_notes: c.source_notes, evidence: c.item.evidence }),
      ),
    ],
  };
  return [
    button(
      "返回题目队列",
      "back",
      () => (location.hash = "questions"),
      "button quiet",
    ),
    el(
      "div",
      { className: "detail-summary" },
      el(
        "div",
        {},
        el("div", { className: "eyebrow" }, "QUESTION VERSION"),
        el("h1", { className: "mono" }, q.id),
      ),
      chip(q.status),
      fields([
        [
          "学科 / 子类",
          `${labels[q.category]?.[0] || q.category} / ${q.subtopic}`,
        ],
        ["代次", `R${q.generation}`],
      ]),
      sampleCell(q),
    ),
    tabs([
      ["question", "题面"],
      ["golden", "Golden 与推导"],
      ["quality", "质检证据"],
      ["samples", "GLM 样本"],
      ["lineage", "来源与谱系"],
    ]),
    ...(sections[tab] || sections.question)(),
  ];
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    toast("浏览器未开放剪贴板，请手动选择文本复制。");
  }
}
function toast(message) {
  const node = el("div", { className: "toast", role: "status" }, message);
  document.body.append(node);
  setTimeout(() => node.remove(), 2600);
}
function runDetail(data) {
  const rows = state.data.questions.filter((q) => q.run_id === data.id);
  return [
    button(
      "返回运行批次",
      "back",
      () => (location.hash = "runs"),
      "button quiet",
    ),
    heading(data.id, "当前数据库的运行身份与协议快照。", chip(data.status)),
    panel(
      "冻结身份",
      null,
      fields([
        ["模式", data.mode],
        ["Fingerprint", data.fingerprint],
      ]),
    ),
    el(
      "div",
      { className: "overview-grid" },
      panel(
        "模型角色",
        null,
        table(
          ["职责", "配置模型", "Reasoning"],
          Object.entries(data.models).map(([role, m]) => [
            roleLabels[role],
            m.model || "未记录",
            m.reasoning_effort || "—",
          ]),
        ),
      ),
      panel("冻结协议", null, jsonView(data.protocol)),
    ),
    panel(
      "关联题目",
      link("在队列中筛选 →", `#questions?run=${encodeURIComponent(data.id)}`),
      table(
        ["题目 / ID", "学科与子类", "代次", "状态", "正确 / 已记录", "操作"],
        rows.map(questionCells),
      ),
    ),
    panel("提示词哈希", null, jsonView(data.prompt_hashes)),
  ];
}
function archiveDetail(data) {
  const r = data.report;
  return [
    button(
      "返回实验档案",
      "back",
      () => (location.hash = "archives"),
      "button quiet",
    ),
    heading(
      r.run_id,
      "历史报告 · 保留原版本和原结论。",
      button("下载原报告", "download", () => download(r, r.run_id)),
    ),
    el(
      "div",
      { className: "notice" },
      "此页来自脱敏档案文件 " +
        data.filename +
        "。历史数据不表示当前数据库的实时运行状态。",
    ),
    panel(
      "实验身份",
      chip(r.status || "unknown"),
      fields([
        ["结束时间", date(r.finalized_at)],
        ["源代码", r.source_code || r.code_commit || "未记录"],
        ["交付留存", fmt(r.delivery_items)],
      ]),
    ),
    r.limitations && panel("覆盖边界", null, jsonView(r.limitations)),
    r.interpretation &&
      panel(
        "结论",
        null,
        typeof r.interpretation === "string"
          ? prose(r.interpretation)
          : jsonView(r.interpretation),
      ),
    panel("原始脱敏报告", null, jsonView(r)),
  ];
}
function render() {
  const active = document.activeElement,
    focusId = active?.id,
    cursor = active?.selectionStart;
  const main = el("main", { id: "main", tabindex: "-1" }, topbar());
  const body = el("div", { className: "workspace" });
  if (state.error)
    body.append(
      el(
        "div",
        { className: "error", role: "alert" },
        el("strong", {}, "数据同步失败"),
        el("span", {}, state.error),
        state.data &&
          el(
            "span",
            {},
            " 当前保留上次快照，更新时间 " + date(state.data.generated_at),
          ),
        button("重新连接", "refresh", () => refresh()),
      ),
    );
  if (!state.data)
    body.append(
      empty(
        state.error ? "等待数据连接" : "正在载入工作空间…",
        state.error
          ? "请检查服务启动参数和数据库连接。"
          : "读取批次、题目与材料索引。",
      ),
    );
  else {
    state.data.warnings.forEach((w) =>
      body.append(el("div", { className: "notice" }, w)),
    );
    const { page, id } = current();
    let content;
    if (page === "question" && id) content = detailPage("questions", id);
    else if (page === "run" && id) content = detailPage("runs", id);
    else if (page === "archive" && id) content = detailPage("archives", id);
    else
      content = (
        { overview, questions, runs, papers, quality, archives, costs }[page] ||
        overview
      )();
    body.append(...[content].flat(Infinity).filter(Boolean));
    body.append(
      el(
        "div",
        { className: "page-footer" },
        el("span", {}, "RST-HLE · 论文启发出题与审查"),
        el("span", {}, `快照 ${date(state.data.generated_at)} · 每 15 秒刷新`),
      ),
    );
  }
  main.append(body);
  const root = el(
    "div",
    { className: collapsed ? "console collapsed" : "console" },
    sidebar(),
    main,
  );
  $("#app").replaceChildren(root);
  if (focusId) {
    const field = document.getElementById(focusId);
    if (field) {
      field.focus();
      if (cursor != null && field.setSelectionRange)
        field.setSelectionRange(cursor, cursor);
    }
  }
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  const id = ++requestId;
  try {
    const response = await fetch("/api/snapshot", {
      cache: "no-store",
      signal: AbortSignal.timeout(20000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "服务返回错误");
    if (id === requestId) {
      state.data = data;
      state.error = "";
    }
  } catch (error) {
    if (id === requestId)
      state.error =
        error.name === "TimeoutError" ? "读取超时，请重试。" : error.message;
  } finally {
    if (id === requestId) {
      state.loading = false;
      render();
      const r = current();
      if (r.id && ["question", "run"].includes(r.page))
        getDetail(r.page === "question" ? "questions" : "runs", r.id, true);
    }
  }
}
window.addEventListener("hashchange", () => {
  if (!["question", "run", "archive"].includes(current().page)) {
    state.detailKey = "";
    state.detail = null;
  }
  if (current().page === "questions") {
    state.filters.questions = {
      ...state.filters.questions,
      status: current().query.get("status") || "",
      page: 1,
    };
  }
  render();
  window.scrollTo(0, 0);
});
document.querySelector(".skip").addEventListener("click", (e) => {
  e.preventDefault();
  $("#main").focus();
  $("#main").scrollIntoView();
});
if (current().page === "questions")
  state.filters.questions = { status: current().query.get("status") || "" };
setInterval(() => {
  if (!document.hidden) refresh();
}, 15000);
render();
refresh();
