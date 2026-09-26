/* fcli server UI: live SR Linux report tables fed by a server-sent event stream. */
(() => {
  "use strict";

  const WINDOW_STEP = 250; // rows appended per scroll batch
  const COL_WIDTH_MIN = 56;
  const COL_WIDTH_DEFAULT = 150;

  // An interface an ethernet-segment holds down on purpose. It reports both
  // halves of the truth - the port is down, and standby is why - so it is
  // neither counted as a fault nor read as forwarding.
  const STANDBY_STATE = "down/standby";

  const STATE_CLASSES = {
    up: "state-up",
    down: "state-down",
    [STANDBY_STATE]: "state-standby",
    enable: "state-enable",
    disable: "state-disable",
    established: "state-established",
    active: "state-down",
    idle: "state-down",
    connect: "state-down",
    opensent: "state-down",
    openconfirm: "state-down",
    inactive: "state-inactive",
  };

  // Some rows are read by their verdict before their content - a finding by
  // how bad it is, a BGP session by whether it is up. The whole row carries
  // that as a tone taken from one column, and that column's cell names it.
  const ROW_TONES = {
    checks: { column: "Severity", tone: (severity) => severity }, // error / warning
    // An acknowledged incident is known, and drawn without its colour.
    incidents: { column: "Severity", tone: (severity) => severity, quiet: (row) => Boolean(row.Ack) },
    // A recovery reads as good news; something merely new or gone as neither.
    changes: { column: "Severity", tone: (severity) => (severity === "info" ? "" : severity) },
    bgp_peers: { column: "state", tone: (session) => (session ? (session === "up" ? "ok" : "down") : "") },
  };

  const el = (id) => document.getElementById(id);
  const dom = {
    reportSearch: el("report-search"),
    reportList: el("report-list"),
    sideSplit: el("side-split"),
    sideSplitter: el("side-splitter"),
    nodesBlock: el("nodes-block"),
    nodeList: el("node-list"),
    nodeSummary: el("node-summary"),
    topoBadge: el("topo-badge"),
    version: el("version"),
    navBack: el("nav-back"),
    navForward: el("nav-forward"),
    title: el("report-title"),
    desc: el("report-desc"),
    liveDot: el("live-dot"),
    liveLabel: el("live-label"),
    globalSearch: el("global-search"),
    invFilter: el("inv-filter"),
    reportParams: el("report-params"),
    clearFiltersBtn: el("clear-filters-btn"),
    filterBadge: el("filter-badge"),
    refresh: el("refresh"),
    pause: el("pause"),
    compareBtn: el("compare-btn"),
    compareMenu: el("compare-menu"),
    diffBar: el("diff-bar"),
    diffLabel: el("diff-label"),
    diffCounts: el("diff-counts"),
    diffSame: el("diff-same"),
    diffExit: el("diff-exit"),
    columnsBtn: el("columns-btn"),
    columnsMenu: el("columns-menu"),
    exportBtn: el("export-btn"),
    exportMenu: el("export-menu"),
    errors: el("errors"),
    tableWrap: el("table-wrap"),
    overviewDashboard: el("overview-dashboard"),
    topologyView: el("topology-view"),
    topoCanvas: el("topo-canvas"),
    topoLegend: el("topo-legend"),
    topoStats: el("topo-stats"),
    topoTabs: el("topo-tabs"),
    topoDetail: el("topo-detail"),
    topoPortLabels: el("topo-port-labels"),
    topoMaxBw: el("topo-max-bw"),
    topoMaxBwUnit: el("topo-max-bw-unit"),
    topoHeatScale: el("topo-heat-scale"),
    topoZoomIn: el("topo-zoom-in"),
    topoZoomOut: el("topo-zoom-out"),
    topoZoomLevel: el("topo-zoom-level"),
    topoZoomFit: el("topo-zoom-fit"),
    topoExportDrawio: el("topo-export-drawio"),
    servicesTreeView: el("services-tree-view"),
    pathGraphView: el("path-graph-view"),
    viewModeBtn: el("view-mode-btn"),
    baselineBtn: el("baseline-btn"),
    ackAllBtn: el("ack-all-btn"),
    watchWrap: el("watch-wrap"),
    watchBtn: el("watch-btn"),
    watchMenu: el("watch-menu"),
    topoOverlay: el("topo-overlay"),
    topoSummary: el("topo-summary"),
    chatTriage: el("chat-triage"),
    kpiCardHealth: el("kpi-card-health"),
    kpiHealthValue: el("kpi-health-value"),
    kpiHealthSub: el("kpi-health-sub"),
    kpiHealthWorst: el("kpi-health-worst"),
    kpiHealthChanges: el("kpi-health-changes"),
    headRow: el("head-row"),
    filterRow: el("filter-row"),
    gridCols: el("grid-cols"),
    body: el("grid-body"),
    empty: el("empty"),
    rowCount: el("row-count"),
    streamInfo: el("stream-info"),
    updated: el("updated"),
    themeToggle: el("theme-toggle"),
    chatOpen: el("chat-open"),
    chatClose: el("chat-close"),
    chatDrawer: el("chat-drawer"),
    chatLog: el("chat-log"),
    chatForm: el("chat-form"),
    chatInput: el("chat-input"),
    chatSend: el("chat-send"),
    chatProvider: el("chat-provider"),
    chatEffort: el("chat-effort"),
    chatResizer: el("chat-resizer"),
    sideResizer: el("side-resizer"),
    // KPI elements
    kpiCardNodes: el("kpi-card-nodes"),
    kpiCardBgp: el("kpi-card-bgp"),
    kpiCardItf: el("kpi-card-itf"),
    kpiNodesTotal: el("kpi-nodes-total"),
    kpiNodesConnected: el("kpi-nodes-connected"),
    kpiNodesStreaming: el("kpi-nodes-streaming"),
    kpiNodesUnreachable: el("kpi-nodes-unreachable"),
    kpiBgpEstablished: el("kpi-bgp-established"),
    kpiBgpSub: el("kpi-bgp-sub"),
    kpiBgpTotal: el("kpi-bgp-total"),
    kpiBgpDown: el("kpi-bgp-down"),
    kpiItfTotal: el("kpi-itf-total"),
    kpiItfDown: el("kpi-itf-down"),
    kpiItfErrors: el("kpi-itf-errors"),
    kpiCardBd: el("kpi-card-bd"),
    kpiBdTotal: el("kpi-bd-total"),
    kpiBdUp: el("kpi-bd-up"),
    kpiBdDegraded: el("kpi-bd-degraded"),
    kpiBdDown: el("kpi-bd-down"),
    kpiBdInstances: el("kpi-bd-instances"),
    kpiCardRouters: el("kpi-card-routers"),
    kpiRoutersTotal: el("kpi-routers-total"),
    kpiRoutersUp: el("kpi-routers-up"),
    kpiRoutersDegraded: el("kpi-routers-degraded"),
    kpiRoutersDown: el("kpi-routers-down"),
    kpiRoutersInstances: el("kpi-routers-instances"),
    kpiSubCount: el("kpi-sub-count"),
    kpiCacheCount: el("kpi-cache-count"),
    kpiResyncInt: el("kpi-resync-int"),
  };

  const state = {
    reports: [],
    report: null,
    columns: [],
    rows: [],
    errors: [],
    hidden: new Set(),
    colFilters: new Map(),
    colWidths: new Map(),
    reportParams: new Map(), // the selected report's own arguments, e.g. the RIB LPM address
    networkInstances: [], // the fabric's instances, for an argument that is one of them
    tree: null, // a lens's answer as cards, alongside its rows
    records: null, // a lens's answer as the objects it found
    graph: null, // a lens's answer as a graph, where it has one (the path walk)
    sort: { column: null, dir: 1 },
    windowSize: WINDOW_STEP,
    paused: false,
    source: null,
    previous: new Map(), // row identity -> previous row values
    identityColumn: null,
    firstPaint: true,
    viewMode: "tree",
    topology: null,
    topoSelection: null,
    topoKey: "",
    topoSize: null, // the drawing in its own units, before any zoom
    topoZoom: 1,
    topoFit: true,
    topoFabric: null, // the fabric being drawn, or "all"
    // What the drawing is coloured by: "traffic", "health", or "service:<name>".
    topoOverlay: "traffic",
    collapsedCards: new Set(),
    collapsedNodes: new Set(),
    // Incident cards already given their starting collapse, by incident and
    // whether it is acknowledged: a card starts collapsed, and collapses again
    // when it is acknowledged.
    seenIncidentCards: new Set(),
    collapsedSections: new Set(),
    navStack: [],
    navIndex: -1,
    chatEnabled: false,
    chatBusy: false,
    chatMessages: [],
    chatAbort: null,
    chatProviders: [],
    chatProvider: null,
    chatEffort: null,
    chatWidth: 360,
    // A comparison replaces the live table while it is on screen: the rows
    // below are a verdict on two renderings, and a stream pushing fresh ones
    // over them would be reading as live something that is not.
    diff: null, // { against, nodes, labels, counts, keyed }
    snapshots: [],
    // The containerlab topology this server was started on, when it has a
    // name. Snapshots of another fabric are marked as such in the menu.
    fabric: "",
  };

  let overviewTimer = null;
  let topologyTimer = null;
  let navSeq = 0;

  /* ------------------------------------------------------------ helpers */

  const debounce = (fn, ms) => {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  };

  /** Build a case-insensitive matcher; falls back to substring on a bad regex. */
  function matcher(pattern) {
    if (!pattern) return null;
    try {
      const re = new RegExp(pattern, "i");
      return (value) => re.test(value);
    } catch (_err) {
      const needle = pattern.toLowerCase();
      return (value) => value.toLowerCase().includes(needle);
    }
  }

  const isNumeric = (value) =>
    value !== "" && value !== null && value !== undefined && !isNaN(Number(value));

  /** Reports the server computes for a panel of their own, not as a table. */
  const isPanelReport = (name) => name === "overview" || name === "topology";

  /** The column a comparison puts its verdict in; matches nornir_srl.diff. */
  const DIFF_STATUS = "\u00b1";

  /** Natural compare, so ethernet-1/10 sorts after ethernet-1/2. */
  const collator = new Intl.Collator(undefined, {
    numeric: true,
    sensitivity: "base",
  });

  function compare(a, b) {
    if (isNumeric(a) && isNumeric(b)) return Number(a) - Number(b);
    return collator.compare(String(a ?? ""), String(b ?? ""));
  }

  /* ------------------------------------------------ persistence */

  function saveReportPreferences() {
    if (!state.report || isPanelReport(state.report.name)) return;
    try {
      localStorage.setItem(
        `fcli-hidden-${state.report.name}`,
        JSON.stringify([...state.hidden])
      );
      localStorage.setItem(
        `fcli-filters-${state.report.name}`,
        JSON.stringify([...state.colFilters.entries()])
      );
      localStorage.setItem(
        `fcli-colwidths-${state.report.name}`,
        JSON.stringify(Object.fromEntries(state.colWidths))
      );
      localStorage.setItem("fcli-global-search", dom.globalSearch.value);
      localStorage.setItem("fcli-inv-filter", dom.invFilter.value);
      localStorage.setItem("fcli-refresh", dom.refresh.value);
    } catch (_err) {
      /* storage unavailable */
    }
  }

  /** Status a BGP RIB table starts filtered on: routes marked used (u). */
  const BGP_RIB_USED_FILTER = "^u";

  /**
   * Column filters a report starts with. Each is applied once per report, on
   * top of whatever filters were saved before; clearing it afterwards is
   * saved like any other change, so a cleared default stays cleared.
   */
  function applyDefaultFilters() {
    if (!state.report.name.startsWith("bgp_rib")) return;
    const key = `fcli-default-filters-${state.report.name}`;
    if (localStorage.getItem(key) === BGP_RIB_USED_FILTER) return;
    state.colFilters.set("st", BGP_RIB_USED_FILTER);
    localStorage.setItem(key, BGP_RIB_USED_FILTER);
    localStorage.setItem(
      `fcli-filters-${state.report.name}`,
      JSON.stringify([...state.colFilters.entries()])
    );
  }

  function loadReportPreferences() {
    if (!state.report || isPanelReport(state.report.name)) return;
    state.hidden.clear();
    state.colFilters.clear();
    state.colWidths.clear();
    try {
      const hiddenData = localStorage.getItem(`fcli-hidden-${state.report.name}`);
      if (hiddenData) {
        JSON.parse(hiddenData).forEach((col) => state.hidden.add(col));
      } else if (["bridge_domains", "services", "routers"].includes(state.report.name)) {
        // Node identifiers shown next to the name in the tree, not as table columns.
        ["System IPv4", "System IPv6", "Gateway", "BGP Instance", "BGP Peers", "Underlay Hosts", "Site"].forEach((c) => state.hidden.add(c));
      }
      const filtersData = localStorage.getItem(`fcli-filters-${state.report.name}`);
      if (filtersData) {
        JSON.parse(filtersData).forEach(([col, val]) => state.colFilters.set(col, val));
      }
      applyDefaultFilters();
      const widthsData = localStorage.getItem(`fcli-colwidths-${state.report.name}`);
      if (widthsData) {
        const parsed = JSON.parse(widthsData);
        if (parsed && typeof parsed === "object") {
          for (const [col, width] of Object.entries(parsed)) {
            const px = Number(width);
            if (px >= COL_WIDTH_MIN) state.colWidths.set(col, px);
          }
        }
      }
    } catch (_err) {
      /* storage unavailable */
    }
  }

  /* ---------------------------------------------------- report arguments */

  // Beyond filtering rows, a report can take arguments of its own: the RIB
  // reports look up an address and keep the longest prefix matching it, the
  // way 'fcli ipv4-rib -a' does. The server applies them to the state it is
  // already streaming, so a change reconnects the stream rather than
  // re-rendering the rows in hand.
  function isLens(report) {
    return Boolean(report && report.kind === "lens");
  }

  // The arguments a lens cannot answer without, still to be typed.
  function missingParams() {
    const specs = (state.report && state.report.params) || [];
    return specs.filter((spec) => spec.required && !state.reportParams.get(spec.name));
  }

  function renderReportParams() {
    dom.reportParams.replaceChildren();
    const specs = (state.report && state.report.params) || [];
    dom.reportParams.hidden = !specs.length;
    for (const spec of specs) {
      const field = document.createElement("label");
      field.className = "field";

      const name = document.createElement("span");
      name.className = "muted";
      name.textContent = spec.required ? `${spec.label} *` : spec.label;

      const input = spec.kind === "ni" ? document.createElement("select") : document.createElement("input");
      input.className = "input";
      input.required = Boolean(spec.required);
      if (spec.help) input.title = spec.help;
      if (spec.kind === "ni") {
        input.dataset.paramKind = "ni";
        input.dataset.paramName = spec.name;
        fillInstanceOptions(input, spec, state.networkInstances);
      } else {
        input.type = "search";
        input.placeholder = spec.placeholder || "";
        input.autocomplete = "off";
        input.spellcheck = false;
        input.value = state.reportParams.get(spec.name) || "";
      }

      input.addEventListener("change", () => {
        const value = input.value.trim();
        const valid = !value || paramIsValid(spec, value);
        input.classList.toggle("is-invalid", !valid);
        input.setAttribute("aria-invalid", valid ? "false" : "true");
        if (!valid) return;
        // Choosing what an unchosen instance already means is choosing nothing.
        if (value && !(spec.kind === "ni" && value === spec.placeholder)) state.reportParams.set(spec.name, value);
        else state.reportParams.delete(spec.name);
        updateFilterUI();
        connect();
        syncCurrentVisit();
      });

      field.append(name, input);
      dom.reportParams.append(field);
    }
    if (specs.some((spec) => spec.kind === "ni")) refreshInstanceOptions();
  }

  // A network-instance is chosen from the ones the fabric has rather than
  // typed. The list arrives after the field is drawn, so what is in hand -
  // the value chosen, or the placeholder, which is what none chosen means -
  // is an option of its own until then, and stays one should the fabric not
  // list it: a link can name an instance the filtered nodes do not carry.
  function fillInstanceOptions(select, spec, instances) {
    const chosen = state.reportParams.get(spec.name) || spec.placeholder || "";
    select.replaceChildren();
    if (chosen && !instances.some((inst) => inst.name === chosen)) {
      select.append(new Option(chosen, chosen));
    }
    const groups = new Map();
    for (const inst of instances) {
      if (!groups.has(inst.type)) groups.set(inst.type, document.createElement("optgroup"));
      const group = groups.get(inst.type);
      group.label = inst.type || "other";
      const option = new Option(inst.name, inst.name);
      option.title = `${inst.type || "instance"} on ${inst.nodes} node${inst.nodes === 1 ? "" : "s"}`;
      group.append(option);
    }
    select.append(...groups.values());
    select.value = chosen;
  }

  async function refreshInstanceOptions() {
    const params = new URLSearchParams();
    const inv = dom.invFilter.value.trim();
    if (inv) params.set("inv_filter", inv);
    try {
      const res = await fetch(`/api/network-instances?${params}`);
      if (!res.ok) return;
      state.networkInstances = (await res.json()).network_instances || [];
    } catch {
      return;
    }
    // Whatever instance fields are on screen by now - the report may have
    // changed while the fabric was being asked, and then there are none.
    const specs = (state.report && state.report.params) || [];
    for (const select of dom.reportParams.querySelectorAll("select[data-param-kind='ni']")) {
      const spec = specs.find((candidate) => candidate.name === select.dataset.paramName);
      if (spec) fillInstanceOptions(select, spec, state.networkInstances);
    }
  }

  // Enough to keep an obvious typo from being sent and answered with a stream
  // that only ever errors. The server validates for real.
  function paramIsValid(spec, value) {
    if (spec.kind !== "address") return true;
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(value)) {
      return value.split(".").every((octet) => Number(octet) <= 255);
    }
    return value.includes(":") && /^[0-9a-f:.]+$/i.test(value);
  }

  function updateFilterUI() {
    let count = 0;
    if (dom.globalSearch.value.trim()) count++;
    if (dom.invFilter.value.trim()) count++;
    count += state.colFilters.size;
    count += state.reportParams.size;
    if (count > 0) {
      dom.clearFiltersBtn.hidden = false;
      dom.filterBadge.textContent = count;
    } else {
      dom.clearFiltersBtn.hidden = true;
      dom.filterBadge.textContent = "";
    }
  }

  function clearAllFilters() {
    dom.globalSearch.value = "";
    dom.invFilter.value = "";
    state.colFilters.clear();
    state.reportParams.clear();
    renderReportParams();
    saveReportPreferences();
    updateFilterUI();
    renderHead();
    renderBody();
    if (!state.report) return;
    if (state.report.name === "topology") loadTopology();
    else if (state.report.name !== "overview") connect();
  }

  /* --------------------------------------------------------- report list */

  async function loadReports() {
    const res = await fetch("/api/reports");
    const data = await res.json();
    state.reports = data.reports;
    state.fabric = data.topo_name || "";
    dom.version.textContent = "v" + data.version;
    if (data.topo_name) {
      if (dom.topoBadge) {
        dom.topoBadge.textContent = "clab: " + data.topo_name;
        dom.topoBadge.title = "Containerlab topology: " + data.topo_name;
        dom.topoBadge.hidden = false;
      }
    } else if (dom.topoBadge) {
      dom.topoBadge.hidden = true;
    }
    if (data.chat && data.chat.enabled && dom.chatOpen) {
      state.chatEnabled = true;
      dom.chatOpen.hidden = false;
      renderChatProviders((data.chat && data.chat.providers) || []);
    } else if (dom.chatOpen) {
      state.chatEnabled = false;
      dom.chatOpen.hidden = true;
      renderChatProviders([]);
      closeChat();
    }
    renderReportList();

    try {
      const savedGlobal = localStorage.getItem("fcli-global-search");
      if (savedGlobal) dom.globalSearch.value = savedGlobal;
      const savedInv = localStorage.getItem("fcli-inv-filter");
      if (savedInv) dom.invFilter.value = savedInv;
      const savedRefresh = localStorage.getItem("fcli-refresh");
      if (savedRefresh) dom.refresh.value = savedRefresh;
    } catch (_err) {}

    // A lens is asked something, and a walk worth sharing is one with its
    // arguments in the link: #path?source=leaf1&destination=10.0.0.1&ni=vrf.
    const [wanted, query] = location.hash.replace(/^#/, "").split("?", 2);
    const initial = state.reports.find((r) => r.name === wanted) || state.reports[0];
    if (initial) {
      selectReport(initial);
      if (query && (initial.params || []).length) {
        const given = new URLSearchParams(query);
        for (const spec of initial.params) {
          const value = (given.get(spec.name) || "").trim();
          if (value) state.reportParams.set(spec.name, value);
        }
        renderReportParams();
        updateFilterUI();
        connect();
        syncCurrentVisit();
      }
    }
  }

  function renderReportList() {
    const needle = dom.reportSearch.value.trim().toLowerCase();
    const groups = new Map();
    for (const report of state.reports) {
      const haystack = `${report.title} ${report.name} ${report.description} ${report.category}`;
      if (needle && !haystack.toLowerCase().includes(needle)) continue;
      if (!groups.has(report.category)) groups.set(report.category, []);
      groups.get(report.category).push(report);
    }
    dom.reportList.replaceChildren();
    for (const [category, reports] of groups) {
      const section = document.createElement("div");
      section.className = "report-group";
      const heading = document.createElement("h3");
      heading.textContent = category;
      section.append(heading);
      for (const report of reports) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "report-item";
        button.textContent = report.title;
        button.title = report.description;
        if (state.report && state.report.name === report.name) {
          button.setAttribute("aria-current", "true");
        }
        button.addEventListener("click", () => selectReport(report));
        section.append(button);
      }
      dom.reportList.append(section);
    }
  }

  async function loadOverview() {
    if (!state.report || state.report.name !== "overview") return;
    try {
      const params = new URLSearchParams();
      const inv = dom.invFilter.value.trim();
      if (inv) params.set("inv_filter", inv);
      const url = "/api/overview" + (inv ? `?${params}` : "");
      const res = await fetch(url);
      const data = await res.json();
      renderHealthKpi(data.health);
      dom.kpiNodesTotal.textContent = data.nodes.total;
      dom.kpiNodesConnected.textContent = `${data.nodes.connected} connected`;
      dom.kpiNodesStreaming.textContent = `${data.nodes.streaming} streaming`;
      dom.kpiNodesUnreachable.textContent = `${data.nodes.unreachable} unreachable`;

      dom.kpiBgpEstablished.textContent = data.bgp.established;
      dom.kpiBgpSub.textContent = `${data.bgp.established} established`;
      dom.kpiBgpTotal.textContent = `${data.bgp.total} total`;
      dom.kpiBgpDown.textContent = data.bgp.down > 0 ? ` · ${data.bgp.down} down` : "";

      if (dom.kpiCardNodes) {
        dom.kpiCardNodes.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.nodes.total > 0) {
          if (data.nodes.connected === data.nodes.total && data.nodes.unreachable === 0) {
            dom.kpiCardNodes.classList.add("kpi-ok");
          } else if (data.nodes.connected === 0) {
            dom.kpiCardNodes.classList.add("kpi-err");
          } else {
            dom.kpiCardNodes.classList.add("kpi-warn");
          }
        }
      }

      if (dom.kpiCardBgp) {
        dom.kpiCardBgp.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.bgp.total > 0) {
          if (data.bgp.established === data.bgp.total && data.bgp.down === 0) {
            dom.kpiCardBgp.classList.add("kpi-ok");
          } else if (data.bgp.established === 0) {
            dom.kpiCardBgp.classList.add("kpi-err");
          } else {
            dom.kpiCardBgp.classList.add("kpi-warn");
          }
        }
      }

      if (dom.kpiCardItf) {
        dom.kpiCardItf.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
        if (data.interfaces.total > 0) {
          if (data.interfaces.down === 0 && data.interfaces.errors === 0) {
            dom.kpiCardItf.classList.add("kpi-ok");
          } else {
            dom.kpiCardItf.classList.add("kpi-warn");
          }
        }
      }

      dom.kpiItfTotal.textContent = data.interfaces.total;
      dom.kpiItfDown.textContent = `${data.interfaces.down} oper down`;
      dom.kpiItfErrors.textContent = `${data.interfaces.errors} errors/discards`;

      if (data.bridge_domains) {
        dom.kpiBdTotal.textContent = data.bridge_domains.total;
        dom.kpiBdUp.textContent = `${data.bridge_domains.up} up`;
        dom.kpiBdDegraded.textContent = data.bridge_domains.degraded > 0 ? ` · ${data.bridge_domains.degraded} degraded` : "";
        dom.kpiBdDown.textContent = data.bridge_domains.down > 0 ? ` · ${data.bridge_domains.down} down` : "";
        dom.kpiBdInstances.textContent = ` (${data.bridge_domains.instances} inst)`;

        if (dom.kpiCardBd) {
          dom.kpiCardBd.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
          if (data.bridge_domains.total > 0) {
            if (data.bridge_domains.down > 0) {
              dom.kpiCardBd.classList.add("kpi-err");
            } else if (data.bridge_domains.degraded > 0) {
              dom.kpiCardBd.classList.add("kpi-warn");
            } else {
              dom.kpiCardBd.classList.add("kpi-ok");
            }
          }
        }
      }

      if (data.routers) {
        dom.kpiRoutersTotal.textContent = data.routers.total;
        dom.kpiRoutersUp.textContent = `${data.routers.up} up`;
        dom.kpiRoutersDegraded.textContent = data.routers.degraded > 0 ? ` · ${data.routers.degraded} degraded` : "";
        dom.kpiRoutersDown.textContent = data.routers.down > 0 ? ` · ${data.routers.down} down` : "";
        dom.kpiRoutersInstances.textContent = ` (${data.routers.instances} inst)`;

        if (dom.kpiCardRouters) {
          dom.kpiCardRouters.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
          if (data.routers.total > 0) {
            if (data.routers.down > 0) {
              dom.kpiCardRouters.classList.add("kpi-err");
            } else if (data.routers.degraded > 0) {
              dom.kpiCardRouters.classList.add("kpi-warn");
            } else {
              dom.kpiCardRouters.classList.add("kpi-ok");
            }
          }
        }
      }

      dom.kpiSubCount.textContent = data.telemetry.subscriptions;
      dom.kpiCacheCount.textContent = `${data.telemetry.cached_tables} cached tables`;
      dom.kpiResyncInt.textContent = `${data.telemetry.resync_interval}s resync`;

      setLive("live", "live");
      dom.rowCount.textContent = "Executive Dashboard";
      dom.streamInfo.textContent = "KPI overview metrics";
      dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
    } catch (_err) {
      setLive("error", "error");
    }
  }

  /** The Fabric Health card: incidents by severity, the worst, and what changed. */
  function renderHealthKpi(health) {
    if (!dom.kpiCardHealth) return;
    dom.kpiCardHealth.classList.remove("kpi-ok", "kpi-warn", "kpi-err");
    if (!health) {
      dom.kpiHealthValue.textContent = "—";
      dom.kpiHealthSub.textContent = "checks not available";
      dom.kpiHealthWorst.textContent = "";
      dom.kpiHealthChanges.textContent = "";
      return;
    }
    dom.kpiHealthValue.textContent = health.incidents;
    const acked = health.acknowledged ? ` · ${health.acknowledged} acknowledged` : "";
    dom.kpiHealthSub.textContent = health.incidents
      ? `open incident(s): ${health.errors} error · ${health.warnings} warning · ${health.findings} findings${acked}`
      : health.acknowledged
        ? `nothing open${acked}`
        : "no findings: every check passes";
    dom.kpiHealthWorst.textContent = health.worst ? `worst: ${health.worst}` : "";
    dom.kpiCardHealth.classList.add(health.errors ? "kpi-err" : health.warnings ? "kpi-warn" : "kpi-ok");
    if (!health.watching) {
      dom.kpiHealthChanges.textContent = "timeline off (--watch-interval 0)";
    } else {
      const baseline = health.baseline_at
        ? ` · baseline ${new Date(health.baseline_at * 1000).toLocaleTimeString()}`
        : " · baseline pending";
      const failures = health.failures_15m ? ` (${health.failures_15m} failures)` : "";
      dom.kpiHealthChanges.textContent = `${health.changes_15m} change(s) in 15 min${failures}${baseline}`;
    }
  }

  /* ------------------------------------------------------------- topology */

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Create an SVG element with its attributes in one call. */
  function svgEl(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    for (const key in attrs || {}) node.setAttribute(key, attrs[key]);
    return node;
  }

  const ROLE_LABELS = {
    client: "Client",
    segment: "Ethernet segment",
    leaf: "Leaf",
    spine: "Spine",
    dcgw: "DCGW",
    core: "WAN / core",
    unknown: "Unclassified",
    external: "Outside inventory",
  };

  const TOPO = {
    nodeHeight: 58,
    minNodeWidth: 96,
    gapX: 26,
    siteGap: 30,
    rowHeight: 134,
    padLeft: 124, // room for the tier label down the left edge
    padRight: 28,
    padY: 24,
  };

  // Low enough that a fabric wide enough to need panning at any readable size
  // can still be taken in whole.
  const TOPO_ZOOM_MIN = 0.05;
  const TOPO_ZOOM_MAX = 4;
  const TOPO_ZOOM_STEP = 1.25;
  // Fractions of the lab-wide per-link capacity. Emulated nodes share one
  // forwarding budget, so every cable is coloured against the same max.
  const TOPO_BW_TH1 = 0.25;
  const TOPO_BW_TH2 = 0.5;
  const TOPO_BW_TH3 = 0.75;
  const TOPO_MAX_BW_DEFAULT = 10;
  const TOPO_MAX_BW_UNIT_DEFAULT = "1000000";

  const shortPort = (port) => String(port || "").replace(/^ethernet-/, "e");

  async function loadTopology() {
    if (!state.report || state.report.name !== "topology" || state.paused) return;
    try {
      const inv = dom.invFilter.value.trim();
      const params = new URLSearchParams();
      if (inv) params.set("inv_filter", inv);
      const res = await fetch("/api/topology" + (inv ? `?${params}` : ""));
      const graph = await res.json();
      setLive("live", "live");
      dom.streamInfo.textContent = `LLDP topology, rendered in ${graph.render_ms} ms`;
      dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
      // Re-drawing would drop the hover and lose the scroll position. Rates
      // change every poll; the cables themselves do not, so colour in place.
      state.topology = graph;
      const key = topoStructureKey(graph);
      if (key === state.topoKey) {
        recolorTopoLinks(graph);
        // The incidents can change while every node keeps its colour.
        renderTopoSummary(graph);
        if (state.topoSelection && state.topoSelection.kind === "link") {
          renderTopoLinkDetail(state.topoSelection.id);
        }
        return;
      }
      state.topoKey = key;
      renderTopology(graph);
    } catch (_err) {
      setLive("error", "error");
    }
  }

  /** Place every node on the tier its role puts it in, one row per tier. */
  function layoutTopology(graph) {
    const byName = new Map(graph.nodes.map((node) => [node.name, node]));
    const rows = [];
    let widest = 0;
    for (const layer of graph.layers) {
      const nodes = layer.nodes.map((name) => byName.get(name)).filter(Boolean);
      let width = 0;
      let site = null;
      const sized = nodes.map((node) => {
        // Wide enough for whichever of the lines in the box is the longer.
        const text = Math.max(
          node.label.length * 8,
          topoNodeSub(node).length * 6.2,
          (node.platform || "").length * 6.2
        );
        const w = Math.max(TOPO.minNodeWidth, text + 28);
        if (width) width += TOPO.gapX;
        if (site !== null && node.site !== site) width += TOPO.siteGap - TOPO.gapX;
        site = node.site;
        const placed = { node, w, offset: width };
        width += w;
        return placed;
      });
      widest = Math.max(widest, width);
      rows.push({ layer, nodes: sized, width });
    }

    const canvasWidth = TOPO.padLeft + widest + TOPO.padRight;
    const positions = new Map();
    rows.forEach((row, index) => {
      const y = TOPO.padY + index * TOPO.rowHeight;
      const start = TOPO.padLeft + (widest - row.width) / 2;
      row.y = y;
      for (const placed of row.nodes) {
        const x = start + placed.offset;
        positions.set(placed.node.name, {
          x,
          y,
          w: placed.w,
          h: TOPO.nodeHeight,
          cx: x + placed.w / 2,
          cy: y + TOPO.nodeHeight / 2,
        });
      }
    });

    return {
      rows,
      positions,
      width: canvasWidth,
      height: TOPO.padY * 2 + rows.length * TOPO.rowHeight,
    };
  }

  function renderTopology(whole) {
    // Everything below draws one fabric; the whole graph stays in state, so a
    // node of another one is still there to be looked up and walked to.
    renderTopoTabs(whole);
    renderTopoSummary(whole);
    renderTopoOverlayOptions(whole);
    const graph = topoOverlayView(topoFabricView(whole));
    dom.topoCanvas.replaceChildren();
    renderTopoLegend(graph);
    renderTopoHeatLegend();
    dom.topoStats.textContent = topoSummary(graph);
    dom.rowCount.textContent = `${graph.nodes.length} node(s), ${graph.links.length} link(s)`;
    if (dom.topoExportDrawio) dom.topoExportDrawio.disabled = !graph.nodes.length;
    if (!graph.nodes.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No nodes are streaming LLDP yet.";
      dom.topoCanvas.append(empty);
      state.topoSize = null;
      applyTopoZoom();
      return;
    }

    const layout = layoutTopology(graph);
    state.topoSize = { width: layout.width, height: layout.height };
    const svg = svgEl("svg", {
      class: "topo-svg",
      viewBox: `0 0 ${layout.width} ${layout.height}`,
      role: "img",
      "aria-label": "Fabric topology",
    });

    const bands = svgEl("g", { class: "topo-bands" });
    for (const row of layout.rows) {
      bands.append(
        svgEl("rect", {
          class: "topo-band",
          x: 8,
          y: row.y - 18,
          width: layout.width - 16,
          height: TOPO.nodeHeight + 36,
          rx: 10,
        })
      );
      const label = svgEl("text", {
        class: "topo-band-label",
        x: 20,
        y: row.y + TOPO.nodeHeight / 2 + 4,
      });
      label.textContent = row.layer.label;
      bands.append(label);
    }
    svg.append(bands);

    svg.append(renderTopoLinks(graph, layout));
    svg.append(renderTopoNodes(graph, layout));
    dom.topoCanvas.append(svg);
    applyTopoService(svg, graph);
    applyTopoZoom();

    svg.addEventListener("mouseover", (event) => {
      const target = event.target.closest("[data-node]");
      highlightTopo(target ? target.dataset.node : null);
    });
    svg.addEventListener("mouseleave", () => highlightTopo(null));
    svg.addEventListener("click", (event) => {
      const node = event.target.closest("[data-node]");
      const link = event.target.closest("[data-link]");
      if (node) selectTopo({ kind: "node", id: node.dataset.node });
      else if (link) selectTopo({ kind: "link", id: link.dataset.link });
      else selectTopo(null);
    });

    applyTopoSelection();
  }

  /* -------------------------------------------------------- topology health */

  /** The fabric briefed in a few lines, the last of them what is wrong. */
  function renderTopoSummary(graph) {
    if (!dom.topoSummary) return;
    const lines = graph.summary || [];
    dom.topoSummary.replaceChildren();
    dom.topoSummary.hidden = !lines.length;
    const incidents = (graph.incidents || []).filter((i) => !i.acknowledged);
    lines.forEach((text, index) => {
      const line = document.createElement("span");
      line.className = "topo-summary-line";
      line.textContent = text;
      const last = index === lines.length - 1 && "incidents" in graph;
      if (last) {
        const worst = incidents.some((i) => i.severity === "error")
          ? "error"
          : incidents.length
            ? "warning"
            : "ok";
        line.classList.add("is-health", `tone-${worst}`);
        if (incidents.length) {
          const open = document.createElement("button");
          open.type = "button";
          open.className = "topo-summary-link";
          open.textContent = "open incidents";
          open.addEventListener("click", () => {
            const report = state.reports.find((r) => r.name === "incidents");
            if (report) selectReport(report);
          });
          line.append(" ", open);
        }
      }
      dom.topoSummary.append(line);
    });
  }

  /** Traffic, health, and one entry per service the fabric carries. */
  function renderTopoOverlayOptions(graph) {
    if (!dom.topoOverlay) return;
    const services = [
      ...new Set((graph.nodes || []).flatMap((node) => node.services || [])),
    ].sort();
    const wanted = ["traffic", "health", ...services.map((name) => `service:${name}`)];
    const have = [...dom.topoOverlay.options].map((option) => option.value);
    if (wanted.join("\u0000") !== have.join("\u0000")) {
      dom.topoOverlay.replaceChildren(new Option("traffic", "traffic"), new Option("health", "health"));
      if (services.length) {
        const group = document.createElement("optgroup");
        group.label = "service";
        for (const name of services) group.append(new Option(name, `service:${name}`));
        dom.topoOverlay.append(group);
      }
    }
    if (!wanted.includes(state.topoOverlay)) state.topoOverlay = "traffic";
    dom.topoOverlay.value = state.topoOverlay;
    // The heat scale only means something while traffic is what is drawn.
    const heat = document.getElementById("topo-heat");
    if (heat) heat.hidden = state.topoOverlay !== "traffic";
  }

  /** Light up what carries the chosen service; everything else steps back. */
  function applyTopoService(svg, graph) {
    const overlay = state.topoOverlay || "";
    const service = overlay.startsWith("service:") ? overlay.slice("service:".length) : null;
    svg.classList.toggle("is-service", Boolean(service));
    if (!service) return;
    const carrying = new Set(
      (graph.nodes || [])
        .filter(
          (node) =>
            (node.services || []).includes(service) ||
            (node.attachments || []).some((a) => a.service === service)
        )
        .map((node) => node.name)
    );
    // The client whose address a virtual segment tracks is part of the
    // routed service through it, whatever bridge domain it sits in.
    for (const link of graph.links || []) {
      if (link.kind !== "ves-nh") continue;
      if (carrying.has(link.a)) carrying.add(link.b);
      if (carrying.has(link.b)) carrying.add(link.a);
    }
    svg.querySelectorAll("[data-node]").forEach((cell) => {
      cell.classList.toggle("in-service", carrying.has(cell.dataset.node));
    });
    svg.querySelectorAll("[data-link]").forEach((pair) => {
      pair.classList.toggle("in-service", carrying.has(pair.dataset.a) && carrying.has(pair.dataset.b));
    });
  }

  /** How many findings a node has, on its top-right corner, in the worst one's colour. */
  function topoBadge(node, box) {
    const counts = node.findings || {};
    const total = (counts.error || 0) + (counts.warning || 0);
    if (!total) return null;
    const tone = counts.error ? "error" : "warning";
    const badge = svgEl("g", { class: `topo-badge tone-${tone}` });
    const cx = box.x + box.w - 2;
    const cy = box.y + 2;
    badge.append(svgEl("circle", { cx, cy, r: total > 9 ? 10 : 8 }));
    const text = svgEl("text", { x: cx, y: cy + 3.5, "text-anchor": "middle" });
    text.textContent = total > 99 ? "99+" : String(total);
    badge.append(text);
    return badge;
  }

  /** What the checks found, as a list in a detail panel. */
  function appendTopoIssues(panel, issues, noun) {
    if (!issues.length) return;
    const heading = document.createElement("h3");
    heading.textContent = `${issues.length} ${noun}`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-issue-list";
    for (const issue of issues) {
      const item = document.createElement("li");
      item.className = `topo-issue tone-${issue.severity}${issue.acknowledged ? " is-acked" : ""}`;
      const head = document.createElement("div");
      head.className = "topo-issue-head";
      head.textContent = `${issue.acknowledged ? "✓ " : ""}${issue.node} ${issue.check} ${issue.subject}`;
      if (issue.acknowledged) head.title = "acknowledged";
      const detail = document.createElement("div");
      detail.className = "muted";
      detail.textContent = issue.detail;
      item.append(head, detail);
      list.append(item);
    }
    panel.append(list);
  }

  /**
   * The graph without what only one service overlay draws: a virtual
   * ethernet-segment and its aliasing are not cables, and belong on the
   * drawing only while the routed service they serve is the one shown.
   */
  function topoOverlayView(graph) {
    const overlay = state.topoOverlay || "";
    const service = overlay.startsWith("service:") ? overlay.slice("service:".length) : null;
    const shown = (item) => !item.overlay_only || (service !== null && item.overlay_only.includes(service));
    const nodes = graph.nodes.filter(shown);
    const names = new Set(nodes.map((node) => node.name));
    return {
      ...graph,
      nodes,
      links: graph.links.filter((link) => shown(link) && names.has(link.a) && names.has(link.b)),
      layers: graph.layers
        .map((layer) => ({ ...layer, nodes: layer.nodes.filter((name) => names.has(name)) }))
        .filter((layer) => layer.nodes.length),
    };
  }

  /* ------------------------------------------------------- topology fabrics */

  const topoFabrics = (graph) => (graph && graph.fabrics) || [];

  /** The fabric a tab is on, falling back to the largest one it could be. */
  function currentTopoFabric(graph) {
    const fabrics = topoFabrics(graph);
    if (fabrics.length < 2) return "all";
    if (state.topoFabric === "all") return "all";
    if (fabrics.some((fabric) => fabric.id === state.topoFabric)) return state.topoFabric;
    return fabrics[0].id;
  }

  /** The graph reduced to the fabric on the tab, layers and legend with it. */
  function topoFabricView(graph) {
    const id = currentTopoFabric(graph);
    if (id === "all") return graph;
    const nodes = graph.nodes.filter((node) => (node.fabrics || []).includes(id));
    const names = new Set(nodes.map((node) => node.name));
    return {
      ...graph,
      nodes,
      links: graph.links.filter((link) => names.has(link.a) && names.has(link.b)),
      layers: graph.layers
        .map((layer) => ({ ...layer, nodes: layer.nodes.filter((name) => names.has(name)) }))
        .filter((layer) => layer.nodes.length),
      roles: nodes.reduce((counts, node) => {
        counts[node.role] = (counts[node.role] || 0) + 1;
        return counts;
      }, {}),
      unresolved: graph.unresolved.filter((entry) => names.has(entry.peer)),
    };
  }

  function renderTopoTabs(graph) {
    const fabrics = topoFabrics(graph);
    dom.topoTabs.replaceChildren();
    // One fabric is the whole drawing; a tab strip of one says nothing.
    dom.topoTabs.hidden = fabrics.length < 2;
    if (fabrics.length < 2) return;
    const current = currentTopoFabric(graph);
    state.topoFabric = current;
    const tabs = fabrics.map((fabric) => ({
      id: fabric.id,
      label: fabric.label,
      // The nodes of the fabric rather than the boxes drawn for it: the clients
      // are counted again on every other fabric they are plugged into.
      count: `${fabric.devices} devices`,
      title: topoFabricMembers(graph, fabric.id),
    }));
    tabs.push({
      id: "all",
      label: "All",
      count: `${fabrics.length} fabrics`,
      title: "Every fabric at once, side by side",
    });
    for (const tab of tabs) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-tab" + (tab.id === current ? " is-active" : "");
      button.title = tab.title;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(tab.id === current));
      const label = document.createElement("span");
      label.textContent = tab.label;
      const count = document.createElement("span");
      count.className = "muted";
      count.textContent = tab.count;
      button.append(label, count);
      button.addEventListener("click", () => setTopoFabric(tab.id));
      dom.topoTabs.append(button);
    }
  }

  /** The nodes on a tab, for a tab whose name cannot say which fabric it is. */
  function topoFabricMembers(graph, id) {
    const names = graph.nodes
      .filter((node) => !isTopoAttached(node) && (node.fabrics || []).includes(id))
      .map((node) => node.label);
    const shown = names.slice(0, 6).join(", ");
    return names.length > 6 ? `${shown} and ${names.length - 6} more` : shown;
  }

  function setTopoFabric(id) {
    if (id === state.topoFabric) return;
    state.topoFabric = id;
    try {
      localStorage.setItem("fcli-topo-fabric", id);
    } catch (_err) {
      /* storage may be unavailable */
    }
    dom.topoCanvas.scrollLeft = 0;
    dom.topoCanvas.scrollTop = 0;
    if (state.topology) renderTopology(state.topology);
  }

  /** The fabric holding *name*, when the one on the tab does not. */
  function topoFabricElsewhere(name) {
    if (!state.topology) return null;
    const current = currentTopoFabric(state.topology);
    if (current === "all") return null;
    const node = (state.topology.nodes || []).find((entry) => entry.name === name);
    const fabrics = (node && node.fabrics) || [];
    if (!fabrics.length || fabrics.includes(current)) return null;
    return fabrics[0];
  }

  /* --------------------------------------------------------- topology zoom */

  const clampZoom = (zoom) =>
    Number.isFinite(zoom) ? Math.min(TOPO_ZOOM_MAX, Math.max(TOPO_ZOOM_MIN, zoom)) : 1;

  /** The zoom in force: the fitted one while fit mode is on, else the picked one. */
  const topoZoom = () => (state.topoFit ? topoFitZoom() : state.topoZoom);

  /** The zoom at which the whole drawing fits the canvas, never magnifying it. */
  function topoFitZoom() {
    const size = state.topoSize;
    if (!size || !size.width || !size.height) return 1;
    const style = getComputedStyle(dom.topoCanvas);
    const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const padY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const width = dom.topoCanvas.clientWidth - padX;
    const height = dom.topoCanvas.clientHeight - padY;
    if (width <= 0 || height <= 0) return 1;
    return clampZoom(Math.min(1, width / size.width, height / size.height));
  }

  /** Size the drawing to the current zoom and put the buttons in step with it. */
  function applyTopoZoom() {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    const zoom = topoZoom();
    if (svg && state.topoSize) {
      svg.style.width = `${Math.round(state.topoSize.width * zoom)}px`;
      svg.style.height = `${Math.round(state.topoSize.height * zoom)}px`;
    }
    dom.topoZoomLevel.textContent = `${Math.round(zoom * 100)}%`;
    dom.topoZoomFit.classList.toggle("is-active", state.topoFit);
  }

  /**
   * Zoom to *zoom*, holding still whatever is under *anchor* (a viewport point,
   * the pointer or the middle of the canvas), so the fabric does not slide out
   * from under the part being read.
   */
  function setTopoZoom(zoom, anchor) {
    const before = topoZoom();
    const after = clampZoom(zoom);
    state.topoFit = false;
    state.topoZoom = after;
    if (after === before) {
      applyTopoZoom();
      saveTopoZoom();
      return;
    }
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    const rect = svg ? svg.getBoundingClientRect() : null;
    const point = rect && { x: (anchor.x - rect.left) / before, y: (anchor.y - rect.top) / before };
    applyTopoZoom();
    if (point) {
      dom.topoCanvas.scrollLeft += point.x * (after - before);
      dom.topoCanvas.scrollTop += point.y * (after - before);
    }
    saveTopoZoom();
  }

  function fitTopoZoom() {
    state.topoFit = true;
    dom.topoCanvas.scrollLeft = 0;
    dom.topoCanvas.scrollTop = 0;
    applyTopoZoom();
    saveTopoZoom();
  }

  /** The middle of the canvas, for zooming that did not start at a pointer. */
  function topoCanvasCenter() {
    const rect = dom.topoCanvas.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  function saveTopoZoom() {
    try {
      localStorage.setItem("fcli-topo-zoom", state.topoFit ? "fit" : String(state.topoZoom));
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function restoreTopoZoom() {
    let stored = null;
    try {
      stored = localStorage.getItem("fcli-topo-zoom");
    } catch (_err) {
      /* storage may be unavailable */
    }
    if (!stored || stored === "fit") return;
    const zoom = parseFloat(stored);
    if (!zoom) return;
    state.topoFit = false;
    state.topoZoom = clampZoom(zoom);
  }

  function restoreTopoFabric() {
    try {
      state.topoFabric = localStorage.getItem("fcli-topo-fabric") || null;
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function renderTopoLinks(graph, layout) {
    const group = svgEl("g", { class: "topo-links" });
    for (const link of graph.links) {
      const a = layout.positions.get(link.a);
      const b = layout.positions.get(link.b);
      if (!a || !b) continue;
      const id = `${link.a}\u0000${link.b}`;
      const pair = svgEl("g", {
        class: `topo-link-pair${link.access ? " is-access" : ""}${link.lost ? " is-lost" : ""}`,
        "data-link": id,
        "data-a": link.a,
        "data-b": link.b,
      });
      const { aIsTop, aPt, bPt } = linkAnchors(a, b);
      let aShape;
      let bShape;
      if (link.intra_layer) {
        // An arc under the tier, so a DCGW mesh or a spine pair does not
        // draw a line straight through the nodes between its ends. Split at
        // the midpoint so each half takes the colour of that end's egress.
        const p0 = { x: a.cx, y: a.y + a.h };
        const p2 = { x: b.cx, y: b.y + b.h };
        const p1 = { x: (a.cx + b.cx) / 2, y: a.y + a.h + 46 };
        const { mid, c1, c2 } = splitQuad(p0, p1, p2);
        aShape = svgEl("path", {
          d: `M ${p0.x} ${p0.y} Q ${c1.x} ${c1.y} ${mid.x} ${mid.y}`,
          fill: "none",
          "data-end": "a",
        });
        bShape = svgEl("path", {
          d: `M ${mid.x} ${mid.y} Q ${c2.x} ${c2.y} ${p2.x} ${p2.y}`,
          fill: "none",
          "data-end": "b",
        });
      } else {
        const mid = midPoint(aPt, bPt);
        aShape = svgEl("line", {
          x1: aPt.x,
          y1: aPt.y,
          x2: mid.x,
          y2: mid.y,
          "data-end": "a",
        });
        bShape = svgEl("line", {
          x1: mid.x,
          y1: mid.y,
          x2: bPt.x,
          y2: bPt.y,
          "data-end": "b",
        });
      }
      aShape.append(svgEl("title"));
      bShape.append(svgEl("title"));
      pair.append(aShape, bShape);
      paintTopoLink(pair, link);

      const [top, bottom] = aIsTop ? [a, b] : [b, a];
      if (link.count > 1) {
        const badge = svgEl("text", {
          class: "topo-link-count",
          x: (a.cx + b.cx) / 2,
          y: (a.cy + b.cy) / 2,
          "text-anchor": "middle",
        });
        badge.textContent = `${link.count}\u00d7`;
        pair.append(badge);
      }
      if (link.kind === "alias" || link.df) {
        const tag = svgEl("text", {
          class: "topo-link-tag",
          x: (a.cx + b.cx) / 2,
          y: (a.cy + b.cy) / 2 - 3,
          "text-anchor": "middle",
        });
        tag.textContent = link.kind === "alias" ? "L3 alias" : "DF";
        pair.append(tag);
      }
      // Only a single cable can be labelled without the two ends colliding;
      // a bundle shows its size instead, and its ports in the detail panel.
      if (dom.topoPortLabels.checked && !link.intra_layer && link.count === 1) {
        const ports = link.ports[0];
        pair.append(
          portLabel(top, bottom, 0.18, shortPort(aIsTop ? ports.a_port : ports.b_port)),
          portLabel(top, bottom, 0.82, shortPort(aIsTop ? ports.b_port : ports.a_port))
        );
      }
      group.append(pair);
    }
    return group;
  }

  /** Attachment points of a cable: bottom of the upper node, top of the lower. */
  function linkAnchors(a, b) {
    const aIsTop = a.y <= b.y;
    return {
      aIsTop,
      aPt: { x: a.cx, y: aIsTop ? a.y + a.h : a.y },
      bPt: { x: b.cx, y: aIsTop ? b.y : b.y + b.h },
    };
  }

  const midPoint = (p, q) => ({ x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 });

  /** Split a quadratic bezier at t=0.5 so each half can take its own stroke. */
  function splitQuad(p0, p1, p2) {
    const c1 = midPoint(p0, p1);
    const c2 = midPoint(p1, p2);
    return { mid: midPoint(c1, c2), c1, c2 };
  }

  /** The lab-wide per-link capacity, in bits per second. */
  function topoMaxLinkBps() {
    const value = parseFloat(dom.topoMaxBw && dom.topoMaxBw.value);
    const unit = parseFloat(dom.topoMaxBwUnit && dom.topoMaxBwUnit.value);
    if (!(value > 0) || !(unit > 0)) return TOPO_MAX_BW_DEFAULT * Number(TOPO_MAX_BW_UNIT_DEFAULT);
    return value * unit;
  }

  function topoBwClass(bps) {
    if (bps == null || !Number.isFinite(Number(bps))) return "bw-none";
    const max = topoMaxLinkBps();
    if (!(max > 0)) return "bw-none";
    const ratio = Number(bps) / max;
    if (ratio < TOPO_BW_TH1) return "bw-green";
    if (ratio < TOPO_BW_TH2) return "bw-yellow";
    if (ratio < TOPO_BW_TH3) return "bw-orange";
    return "bw-red";
  }

  function topoHalfClass(link, bps) {
    const parts = ["topo-link"];
    // What a virtual segment is tied in with is drawn by what it is, not by
    // what it carries: none of it is a cable with a rate of its own.
    if (link.kind) {
      parts.push(`link-${link.kind}`);
      if (link.df) parts.push("is-df");
      if (stateKind(link.state) === "down") parts.push("link-down");
      return parts.join(" ");
    }
    // Coloured by what the checks found on it rather than by what it
    // carries; a down cable stays dashed, which is the other half of it.
    if (state.topoOverlay === "health" && !link.access) {
      if (stateKind(link.state) === "down") parts.push("link-down");
      parts.push(`health-${link.health || "ok"}`);
      return parts.join(" ");
    }
    // A cable that carries nothing gets no bandwidth colour: a down one has
    // nothing to forward, and a standby one is not forwarding on purpose -
    // which is why standby is coloured apart from down rather than red.
    // The class comes from the kind, because "down/standby" is not a name a
    // CSS class can carry.
    const kind = stateKind(link.state);
    if (kind === "down" || kind === "standby") {
      parts.push(`link-${kind}`);
      return parts.join(" ");
    }
    parts.push(`link-${link.state}`);
    parts.push(topoBwClass(bps));
    return parts.join(" ");
  }

  function paintTopoLink(pair, link) {
    const aHalf = pair.querySelector('[data-end="a"]');
    const bHalf = pair.querySelector('[data-end="b"]');
    if (aHalf) aHalf.setAttribute("class", topoHalfClass(link, link.a_out_bps));
    if (bHalf) bHalf.setAttribute("class", topoHalfClass(link, link.b_out_bps));
    const title = topoLinkTitle(link);
    pair.querySelectorAll("title").forEach((node) => {
      node.textContent = title;
    });
  }

  function recolorTopoLinks(graph) {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    renderTopoHeatLegend();
    const byId = new Map((graph.links || []).map((link) => [`${link.a}\u0000${link.b}`, link]));
    svg.querySelectorAll("[data-link]").forEach((pair) => {
      const link = byId.get(pair.getAttribute("data-link"));
      if (link) paintTopoLink(pair, link);
    });
  }

  /** Identity of the drawing, ignoring rates that only recolour it. */
  function topoStructureKey(graph) {
    const links = (graph.links || []).map((link) => ({
      a: link.a,
      b: link.b,
      count: link.count,
      state: link.state,
      intra_layer: link.intra_layer,
      access: link.access,
      ports: (link.ports || []).map((port) => ({ a_port: port.a_port, b_port: port.b_port })),
    }));
    // A finding's detail carries counts that move every sample; the badge
    // and colour a node is drawn with are what the drawing depends on.
    const nodes = (graph.nodes || []).map(({ issues, ...node }) => node);
    return JSON.stringify(nodes) + JSON.stringify(links) + state.topoOverlay;
  }

  function trimBw(text) {
    return String(text).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function formatBps(bps) {
    if (bps == null || !Number.isFinite(Number(bps))) return "—";
    const n = Number(bps);
    if (n >= 1e9) return `${trimBw((n / 1e9).toFixed(2))} Gbps`;
    if (n >= 1e6) return `${trimBw((n / 1e6).toFixed(2))} Mbps`;
    if (n >= 1e3) return `${trimBw((n / 1e3).toFixed(1))} Kbps`;
    return `${Math.round(n)} bps`;
  }

  function renderTopoHeatLegend() {
    if (!dom.topoHeatScale) return;
    const unit = (dom.topoMaxBwUnit && dom.topoMaxBwUnit.selectedOptions[0]
      ? dom.topoMaxBwUnit.selectedOptions[0].textContent
      : "Mbps");
    const value = trimBw(String(dom.topoMaxBw && dom.topoMaxBw.value ? dom.topoMaxBw.value : TOPO_MAX_BW_DEFAULT));
    dom.topoHeatScale.textContent = `of ${value} ${unit}`;
  }

  function saveTopoMaxBw() {
    try {
      localStorage.setItem("fcli-topo-max-bw", dom.topoMaxBw.value);
      localStorage.setItem("fcli-topo-max-bw-unit", dom.topoMaxBwUnit.value);
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  function restoreTopoMaxBw() {
    try {
      const value = localStorage.getItem("fcli-topo-max-bw");
      const unit = localStorage.getItem("fcli-topo-max-bw-unit");
      if (value && dom.topoMaxBw) dom.topoMaxBw.value = value;
      if (unit && dom.topoMaxBwUnit) dom.topoMaxBwUnit.value = unit;
    } catch (_err) {
      /* storage may be unavailable */
    }
    renderTopoHeatLegend();
  }

  function onTopoMaxBwChange() {
    saveTopoMaxBw();
    if (state.topology) recolorTopoLinks(state.topology);
    else renderTopoHeatLegend();
  }

  /** A port name placed a fraction of the way down a link. */
  function portLabel(top, bottom, fraction, text) {
    const label = svgEl("text", {
      class: "topo-port",
      x: top.cx + (bottom.cx - top.cx) * fraction,
      y: top.y + top.h + (bottom.y - top.y - top.h) * fraction,
      "text-anchor": "middle",
    });
    label.textContent = text;
    return label;
  }

  function renderTopoNodes(graph, layout) {
    const group = svgEl("g", { class: "topo-nodes" });
    for (const node of graph.nodes) {
      const box = layout.positions.get(node.name);
      if (!box) continue;
      const cell = svgEl("g", {
        class: `topo-node role-${node.role}${node.connected ? "" : " is-down"}${node.virtual ? " is-virtual" : ""}`,
        "data-node": node.name,
        tabindex: "0",
      });
      cell.append(
        svgEl("rect", { x: box.x, y: box.y, width: box.w, height: box.h, rx: 8 })
      );
      const label = svgEl("text", {
        class: "topo-node-label",
        x: box.cx,
        y: box.y + 18,
        "text-anchor": "middle",
      });
      label.textContent = node.label;
      const sub = svgEl("text", {
        class: "topo-node-sub",
        x: box.cx,
        y: box.y + 33,
        "text-anchor": "middle",
      });
      sub.textContent = topoNodeSub(node);
      cell.append(label, sub);
      if (node.platform) {
        const platform = svgEl("text", {
          class: "topo-node-platform",
          x: box.cx,
          y: box.y + 47,
          "text-anchor": "middle",
        });
        platform.textContent = node.platform;
        cell.append(platform);
      }
      const badge = topoBadge(node, box);
      if (badge) cell.append(badge);
      const title = svgEl("title");
      title.textContent = topoNodeTitle(node);
      cell.append(title);
      group.append(cell);
    }
    return group;
  }

  function topoNodeSub(node) {
    if (node.virtual) {
      const hops = ((node.ves && node.ves.next_hops) || []).map((hop) => hop.address);
      return hops.length ? `nh ${hops.join(", ")}` : "virtual ES";
    }
    if (isTopoAttached(node)) {
      // Being multi-homed is said before what is carried: the leaves under the
      // box are the point of it being a single box.
      const leaves = new Set(node.attachments.map((a) => a.node)).size;
      if (leaves > 1) return `multi-homed, ${leaves} leaves`;
      const services = node.services || [];
      if (services.length === 1) return services[0];
      if (services.length) return `${services.length} services`;
      return `${node.ports} port(s)`;
    }
    if (node.mac_vrfs || node.ip_vrfs) {
      const parts = [];
      if (node.mac_vrfs) parts.push(`${node.mac_vrfs} mac`);
      if (node.ip_vrfs) parts.push(`${node.ip_vrfs} ip`);
      if (node.stitched) parts.push(`${node.stitched} gw`);
      return parts.join(" · ");
    }
    return node.site || ROLE_LABELS[node.role] || node.role;
  }

  /** Whether a node is drawn from its attachments: a client or a segment. */
  function isTopoAttached(node) {
    return node.role === "client" || node.role === "segment";
  }

  function topoNodeTitle(node) {
    const lines = [node.name, ROLE_LABELS[node.role] || node.role];
    if (isTopoAttached(node)) {
      for (const attachment of node.attachments || []) {
        lines.push(attachmentText(attachment));
      }
      return lines.join(" - ");
    }
    if (node.site) lines.push(`site ${node.site}`);
    if (node.platform) lines.push(node.platform);
    if (node.mac_vrfs || node.ip_vrfs) {
      lines.push(`${node.mac_vrfs} mac-vrf, ${node.ip_vrfs} ip-vrf`);
    }
    if (node.stitched) lines.push(`${node.stitched} stitched service(s)`);
    if (node.clients) lines.push(`${node.clients} client(s)`);
    if (node.error) lines.push(node.error);
    const counts = node.findings || {};
    if (counts.error || counts.warning) {
      lines.push(`${counts.error || 0} error / ${counts.warning || 0} warning finding(s)`);
    }
    return lines.join(" - ");
  }

  /** One attachment as a line: where it lands and what it carries. */
  function attachmentText(attachment) {
    const parts = [`${attachment.node} ${shortPort(attachment.subinterface)}`];
    if (attachment.service) parts.push(attachment.service);
    if (attachment.vlan) parts.push(`vlan ${attachment.vlan}`);
    if (attachment.ip) parts.push(attachment.ip);
    return parts.join(" · ");
  }

  function topoLinkTitle(link) {
    const ports = link.ports
      .map((pair) => cableText(pair.a_port, pair.b_port, "↔"))
      .join(", ");
    const parts = [`${link.a} ↔ ${link.b}${ports ? " (" + ports + ")" : ""}`];
    if (link.a_out_bps != null || link.b_out_bps != null) {
      parts.push(`${link.a} out ${formatBps(link.a_out_bps)}`);
      parts.push(`${link.b} out ${formatBps(link.b_out_bps)}`);
    }
    if (link.lost) parts.push("LLDP lost: drawn from what was seen before");
    if (link.note) parts.push(link.note);
    for (const finding of link.findings || []) {
      const ack = finding.acknowledged ? " (acknowledged)" : "";
      parts.push(`${finding.severity}: ${finding.node} ${finding.check} ${finding.subject}${ack}`);
    }
    return parts.join(" · ");
  }

  /** Both ends of a cable, or the one end of it a client does not report. */
  function cableText(near, far, arrow) {
    if (!far) return shortPort(near);
    if (!near) return shortPort(far);
    return `${shortPort(near)} ${arrow} ${shortPort(far)}`;
  }

  function topoSummary(graph) {
    const parts = [`${graph.nodes.length} nodes`, `${graph.links.length} links`];
    if (graph.unresolved.length) {
      parts.push(`${graph.unresolved.length} neighbour(s) outside the inventory`);
    }
    return parts.join(" · ");
  }

  const escapeXml = (text) =>
    String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const parseHex = (hex) => {
    const raw = String(hex || "").replace("#", "");
    if (raw.length === 3) {
      return raw.split("").map((ch) => parseInt(ch + ch, 16));
    }
    if (raw.length !== 6) return [238, 241, 245];
    return [raw.slice(0, 2), raw.slice(2, 4), raw.slice(4, 6)].map((part) => parseInt(part, 16));
  };

  const toHex = ([r, g, b]) =>
    `#${[r, g, b].map((value) => Math.max(0, Math.min(255, value)).toString(16).padStart(2, "0")).join("")}`;

  /** A light tint of *hex*, for draw.io fills that stay readable with dark text. */
  const tintHex = (hex, white = 0.9) => {
    const [r, g, b] = parseHex(hex);
    const color = 1 - white;
    return toHex([
      Math.round(r * color + 255 * white),
      Math.round(g * color + 255 * white),
      Math.round(b * color + 255 * white),
    ]);
  };

  function topoRoleStroke(role) {
    return cssVar(`--role-${role}`) || cssVar("--role-unknown") || "#667085";
  }

  function topoRoleFill(role) {
    return tintHex(topoRoleStroke(role));
  }

  function topoLinkStroke(link) {
    const kind = stateKind(link.state);
    if (kind === "down") return cssVar("--err") || "#b42318";
    if (kind === "standby") return cssVar("--warn") || "#a35b00";
    const bps = Math.max(Number(link.a_out_bps) || 0, Number(link.b_out_bps) || 0);
    const cls = topoBwClass(bps || null);
    const byClass = {
      "bw-green": "--ok",
      "bw-yellow": "--warn",
      "bw-orange": "--heat",
      "bw-red": "--err",
      "bw-none": "--muted",
    };
    return cssVar(byClass[cls] || "--muted") || "#667085";
  }

  function topoNodeDrawioLabel(node) {
    const lines = [
      `<b><font color="#1c2128">${escapeXml(node.label)}</font></b>`,
      `<font color="#667085" style="font-size:10px">${escapeXml(topoNodeSub(node))}</font>`,
    ];
    if (node.platform) {
      lines.push(
        `<font color="#667085" style="font-size:9px">${escapeXml(node.platform)}</font>`
      );
    }
    return lines.join("<br>");
  }

  function topoNodeDrawioStyle(node) {
    const stroke = !node.connected ? cssVar("--err") || "#b42318" : topoRoleStroke(node.role);
    const fill = topoRoleFill(node.role);
    const parts = [
      "rounded=1",
      "whiteSpace=wrap",
      "html=1",
      `fillColor=${fill}`,
      `strokeColor=${stroke}`,
      "fontColor=#1c2128",
      "align=center",
      "verticalAlign=middle",
      "fontSize=11",
      "spacing=6",
    ];
    if (!node.connected || node.role === "external") parts.push("dashed=1");
    if (node.role === "dcgw") parts.push("strokeWidth=2");
    return parts.join(";");
  }

  function topoEdgeDrawioStyle(link, aIsTop) {
    const stroke = topoLinkStroke(link);
    const parts = [
      "html=1",
      "endArrow=none",
      "startArrow=none",
      `strokeColor=${stroke}`,
      "strokeWidth=2",
      "rounded=1",
    ];
    if (link.access) parts.push("dashed=1");
    if (link.intra_layer) {
      parts.push("exitX=0.5", "exitY=1", "entryX=0.5", "entryY=1", "curved=1");
    } else {
      parts.push(
        aIsTop ? "exitX=0.5;exitY=1;entryX=0.5;entryY=0" : "exitX=0.5;exitY=0;entryX=0.5;entryY=1"
      );
    }
    return parts.join(";");
  }

  function buildDrawioXml(graph, layout) {
    let nextId = 2;
    const cells = [
      '<mxCell id="0"/>',
      '<mxCell id="1" parent="0"/>',
    ];
    const nodeIds = new Map();

    const addCell = (attrs, geometry) => {
      const id = String(nextId++);
      const parts = [`<mxCell id="${id}"`];
      for (const [key, value] of Object.entries(attrs)) {
        if (value != null && value !== "") parts.push(`${key}="${escapeXml(value)}"`);
      }
      parts.push(">");
      if (geometry) parts.push(geometry);
      parts.push("</mxCell>");
      cells.push(parts.join(" "));
      return id;
    };

    const bandFill = "#f6f7f9";
    for (const row of layout.rows) {
      addCell(
        {
          parent: "1",
          vertex: "1",
          style: `rounded=1;whiteSpace=wrap;html=1;fillColor=${bandFill};strokeColor=none;opacity=60;`,
        },
        `<mxGeometry x="8" y="${row.y - 18}" width="${layout.width - 16}" height="${
          TOPO.nodeHeight + 36
        }" as="geometry"/>`
      );
      addCell(
        {
          value: row.layer.label,
          parent: "1",
          vertex: "1",
          style: "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontSize=11;fontStyle=1;fontColor=#1c2128;",
        },
        `<mxGeometry x="20" y="${row.y + TOPO.nodeHeight / 2 - 8}" width="100" height="16" as="geometry"/>`
      );
    }

    for (const node of graph.nodes) {
      const box = layout.positions.get(node.name);
      if (!box) continue;
      const id = addCell(
        {
          value: topoNodeDrawioLabel(node),
          parent: "1",
          vertex: "1",
          style: topoNodeDrawioStyle(node),
        },
        `<mxGeometry x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" as="geometry"/>`
      );
      nodeIds.set(node.name, id);
    }

    for (const link of graph.links) {
      const a = layout.positions.get(link.a);
      const b = layout.positions.get(link.b);
      const source = nodeIds.get(link.a);
      const target = nodeIds.get(link.b);
      if (!a || !b || !source || !target) continue;
      const { aIsTop } = linkAnchors(a, b);
      let geometry = '<mxGeometry relative="1" as="geometry"/>';
      if (link.intra_layer) {
        const p0 = { x: a.cx, y: a.y + a.h };
        const p2 = { x: b.cx, y: b.y + b.h };
        const p1 = { x: (a.cx + b.cx) / 2, y: a.y + a.h + 46 };
        geometry = `<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="${p1.x}" y="${p1.y}"/></Array></mxGeometry>`;
      }
      const value =
        dom.topoPortLabels.checked && !link.intra_layer && link.count === 1
          ? escapeXml(
              `${shortPort(link.ports[0].a_port)} ↔ ${shortPort(link.ports[0].b_port)}`
            )
          : link.count > 1
            ? `${link.count}×`
            : "";
      addCell(
        {
          value,
          parent: "1",
          edge: "1",
          source,
          target,
          style: topoEdgeDrawioStyle(link, aIsTop),
        },
        geometry
      );
    }

    const fabricId = currentTopoFabric(graph);
    const fabric =
      fabricId === "all"
        ? "All fabrics"
        : (topoFabrics(graph).find((entry) => entry.id === fabricId) || {}).label || fabricId;
    const diagramName = escapeXml(fabric);

    return (
      `<mxfile host="fcli" agent="fcli topology export" version="22.1.0">` +
      `<diagram id="topology" name="${diagramName}">` +
      `<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="0" arrows="1" fold="1" page="0" pageScale="1" pageWidth="${layout.width}" pageHeight="${layout.height}" math="0" shadow="0">` +
      `<root>${cells.join("")}</root>` +
      `</mxGraphModel>` +
      `</diagram>` +
      `</mxfile>`
    );
  }

  function exportTopologyDrawio() {
    if (!state.topology || !state.topology.nodes.length) return;
    const graph = topoFabricView(state.topology);
    const layout = layoutTopology(graph);
    const xml = buildDrawioXml(graph, layout);
    const blob = new Blob([xml], { type: "application/xml" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const fabricId = currentTopoFabric(state.topology);
    const fabricLabel =
      fabricId === "all"
        ? "all"
        : (topoFabrics(state.topology).find((entry) => entry.id === fabricId) || {}).label ||
          fabricId;
    link.href = url;
    link.download = `topology-${String(fabricLabel).replace(/[^\w.-]+/g, "-")}.drawio`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function renderTopoLegend(graph) {
    dom.topoLegend.replaceChildren();
    const order = ["client", "segment", "leaf", "spine", "dcgw", "core", "unknown", "external"];
    for (const role of order) {
      const count = graph.roles[role];
      if (!count) continue;
      const chip = document.createElement("span");
      chip.className = `topo-chip role-${role}`;
      const swatch = document.createElement("span");
      swatch.className = "topo-swatch";
      const text = document.createElement("span");
      text.textContent = `${ROLE_LABELS[role]} ${count}`;
      chip.append(swatch, text);
      dom.topoLegend.append(chip);
    }
  }

  /** Dim everything that is not *name* and what it is cabled to. */
  function highlightTopo(name) {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    svg.querySelectorAll(".is-hot").forEach((node) => node.classList.remove("is-hot"));
    if (!name) {
      svg.classList.toggle("is-focused", Boolean(state.topoSelection));
      if (state.topoSelection) applyTopoSelection();
      return;
    }
    svg.classList.add("is-focused");
    markTopoNode(svg, name);
  }

  function markTopoNode(svg, name) {
    const node = svg.querySelector(`[data-node="${CSS.escape(name)}"]`);
    if (node) node.classList.add("is-hot");
    svg
      .querySelectorAll(`[data-a="${CSS.escape(name)}"], [data-b="${CSS.escape(name)}"]`)
      .forEach((link) => {
        link.classList.add("is-hot");
        const peer = link.dataset.a === name ? link.dataset.b : link.dataset.a;
        const box = svg.querySelector(`[data-node="${CSS.escape(peer)}"]`);
        if (box) box.classList.add("is-hot");
      });
  }

  function selectTopo(selection) {
    state.topoSelection = selection;
    // Walking the peer list of a client can lead to a node of another fabric,
    // which is drawn on its own tab; go there rather than nowhere.
    if (selection && selection.kind === "node") {
      const elsewhere = topoFabricElsewhere(selection.id);
      if (elsewhere) {
        setTopoFabric(elsewhere);
        return;
      }
    }
    applyTopoSelection();
  }

  function applyTopoSelection() {
    const svg = dom.topoCanvas.querySelector(".topo-svg");
    if (!svg) return;
    svg.querySelectorAll(".is-hot").forEach((node) => node.classList.remove("is-hot"));
    const selection = state.topoSelection;
    svg.classList.toggle("is-focused", Boolean(selection));
    if (!selection) {
      dom.topoDetail.hidden = true;
      dom.topoDetail.replaceChildren();
      return;
    }
    if (selection.kind === "node") {
      markTopoNode(svg, selection.id);
      renderTopoNodeDetail(selection.id);
    } else {
      const link = svg.querySelector(`[data-link="${CSS.escape(selection.id)}"]`);
      if (link) {
        link.classList.add("is-hot");
        for (const end of [link.dataset.a, link.dataset.b]) {
          const box = svg.querySelector(`[data-node="${CSS.escape(end)}"]`);
          if (box) box.classList.add("is-hot");
        }
      }
      renderTopoLinkDetail(selection.id);
    }
  }

  function topoDetailShell(title, subtitle) {
    dom.topoDetail.replaceChildren();
    dom.topoDetail.hidden = false;
    const head = document.createElement("header");
    const heading = document.createElement("h2");
    heading.textContent = title;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn btn-ghost";
    close.textContent = "✕";
    close.title = "Close";
    close.addEventListener("click", () => selectTopo(null));
    head.append(heading, close);
    dom.topoDetail.append(head);
    if (subtitle) {
      const sub = document.createElement("p");
      sub.className = "muted";
      sub.textContent = subtitle;
      dom.topoDetail.append(sub);
    }
    return dom.topoDetail;
  }

  function topoDetailRow(term, description) {
    const row = document.createElement("div");
    row.className = "topo-detail-row";
    const label = document.createElement("span");
    label.className = "muted";
    label.textContent = term;
    const value = document.createElement("span");
    value.textContent = description;
    row.append(label, value);
    return row;
  }

  function renderTopoNodeDetail(name) {
    const graph = state.topology;
    if (!graph) return;
    const node = graph.nodes.find((n) => n.name === name);
    if (!node) {
      // The node went away between two polls; drop the selection with it.
      selectTopo(null);
      return;
    }
    if (isTopoAttached(node)) {
      renderTopoAttachedDetail(node, graph);
      return;
    }
    const panel = topoDetailShell(node.label, node.name === node.label ? "" : node.name);
    panel.append(topoDetailRow("role", ROLE_LABELS[node.role] || node.role));
    if (node.platform) panel.append(topoDetailRow("platform", node.platform));
    if (node.site) panel.append(topoDetailRow("site", node.site));
    panel.append(
      topoDetailRow("services", `${node.mac_vrfs} mac-vrf · ${node.ip_vrfs} ip-vrf`)
    );
    if (node.stitched) {
      panel.append(topoDetailRow("stitched", `${node.stitched} service(s), two bgp-vpn instances`));
    }
    if (node.clients) panel.append(topoDetailRow("clients", String(node.clients)));
    if (node.error) panel.append(topoDetailRow("error", node.error));
    if ((node.services || []).length) panel.append(topoDetailRow("carries", node.services.join(", ")));
    appendTopoIssues(panel, node.issues || [], "finding(s) on this node");

    const links = graph.links.filter((l) => l.a === name || l.b === name);
    const heading = document.createElement("h3");
    heading.textContent = `${links.length} link(s)`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const link of links) {
      const peer = link.a === name ? link.b : link.a;
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-peer";
      const peerNode = graph.nodes.find((n) => n.name === peer);
      const title = document.createElement("span");
      title.textContent = peerNode ? peerNode.label : peer;
      const ports = document.createElement("span");
      ports.className = "muted";
      ports.textContent = link.ports
        .map((pair) =>
          link.a === name
            ? cableText(pair.a_port, pair.b_port, "→")
            : cableText(pair.b_port, pair.a_port, "→")
        )
        .join(", ");
      button.append(title, ports);
      button.addEventListener("click", () => selectTopo({ kind: "node", id: peer }));
      item.append(button);
      list.append(item);
    }
    panel.append(list);
  }

  /** A client or a segment is its attachments: where it lands, in which service. */
  function renderTopoAttachedDetail(node, graph) {
    if (node.virtual) {
      renderTopoVirtualDetail(node, graph);
      return;
    }
    const label = (name) => {
      const peer = graph.nodes.find((n) => n.name === name);
      return peer ? peer.label : name;
    };
    const panel = topoDetailShell(node.label, node.peers.map(label).join(", "));
    panel.append(topoDetailRow("role", ROLE_LABELS[node.role] || node.role));
    if (node.advertised) panel.append(topoDetailRow("lldp name", node.advertised));
    if ((node.names || []).length) {
      panel.append(topoDetailRow("configured as", node.names.join(" · ")));
    }
    if (node.site) panel.append(topoDetailRow("site", node.site));
    const kinds = [...new Set(node.attachments.map((a) => a.kind))];
    if (kinds.length) panel.append(topoDetailRow("attached", kinds.join(" · ")));
    if (node.esi) panel.append(topoDetailRow("esi", node.esi));

    const heading = document.createElement("h3");
    heading.textContent = `${node.attachments.length} attachment(s)`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const attachment of node.attachments) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-peer";
      const title = document.createElement("span");
      title.textContent = `${label(attachment.node)} ${shortPort(attachment.subinterface)}`;
      const detail = document.createElement("span");
      detail.className = "muted";
      const parts = [attachment.service];
      if (attachment.vlan) parts.push(`vlan ${attachment.vlan}`);
      if (attachment.ip) parts.push(attachment.ip);
      if (attachment.state) parts.push(attachment.state);
      detail.textContent = parts.filter(Boolean).join(" · ");
      button.append(title, detail);
      button.addEventListener("click", () =>
        selectTopo({ kind: "node", id: attachment.node })
      );
      item.append(button);
      list.append(item);
    }
    panel.append(list);
  }

  /**
   * A virtual segment: the next-hop it tracks and how that is reached, the
   * leaves it is attached on and which of them is DF, and the remote VTEPs
   * whose route tables load-balance over it.
   */
  function renderTopoVirtualDetail(node, graph) {
    const label = (name) => {
      const peer = graph.nodes.find((n) => n.name === name);
      return peer ? peer.label : name;
    };
    const ves = node.ves || {};
    const panel = topoDetailShell(`vES ${(node.names || []).join(" · ") || node.esi}`, node.esi);
    panel.append(topoDetailRow("serves", (node.services || []).join(", ")));
    panel.append(topoDetailRow("mode", `${ves.mode || "-"} · ${ves.oper || "-"}`));
    for (const hop of ves.next_hops || []) {
      const via = hop.via ? ` via ${hop.via}` : "";
      const evis = (hop.evis || []).length ? ` (evi ${hop.evis.join(", ")})` : "";
      panel.append(topoDetailRow("next-hop", `${hop.address}${via}${evis}`));
    }
    if ((ves.owners || []).length) panel.append(topoDetailRow("owned by", ves.owners.map(label).join(", ")));
    panel.append(topoDetailRow("attached", (ves.attached || []).map(label).join(", ") || "none"));
    const idle = (ves.configured || []).filter((name) => !(ves.attached || []).includes(name));
    if (idle.length) panel.append(topoDetailRow("configured only", idle.map(label).join(", ")));
    for (const [ni, elected] of Object.entries(ves.df || {})) {
      const views = Object.entries((ves.df_views || {})[ni] || {})
        .map(([viewer, df]) => `${viewer}: ${df}`)
        .join(" · ");
      const row = topoDetailRow(`DF ${ni}`, elected.length > 1 ? `CONFLICT - ${views}` : elected.join(", "));
      if (elected.length > 1) row.classList.add("is-conflict");
      panel.append(row);
    }
    const heading = document.createElement("h3");
    heading.textContent = `${(ves.aliasing || []).length} remote VTEP(s) aliasing`;
    panel.append(heading);
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const alias of ves.aliasing || []) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topo-peer";
      const title = document.createElement("span");
      title.textContent = label(alias.node);
      const detail = document.createElement("span");
      detail.className = "muted";
      const over = `ECMP over ${alias.vteps.map(label).join(" + ")}`;
      detail.textContent = alias.prefixes.length ? `${over} · ${alias.prefixes.join(", ")}` : over;
      button.append(title, detail);
      button.addEventListener("click", () => selectTopo({ kind: "node", id: alias.node }));
      item.append(button);
      list.append(item);
    }
    panel.append(list);
  }

  function renderTopoLinkDetail(id) {
    const graph = state.topology;
    if (!graph) return;
    const [a, b] = id.split("\u0000");
    const link = graph.links.find((l) => l.a === a && l.b === b);
    if (!link) {
      selectTopo(null);
      return;
    }
    const label = (name) => {
      const node = graph.nodes.find((n) => n.name === name);
      return node ? node.label : name;
    };
    const panel = topoDetailShell(`${label(a)} ↔ ${label(b)}`, "");
    panel.append(topoDetailRow("state", link.state));
    if (link.lost) panel.append(topoDetailRow("lldp", "lost - drawn from what was seen before"));
    appendTopoIssues(panel, link.findings || [], "finding(s) on this link");
    panel.append(topoDetailRow("cables", String(link.count)));
    panel.append(topoDetailRow(`${label(a)} out`, formatBps(link.a_out_bps)));
    panel.append(topoDetailRow(`${label(b)} out`, formatBps(link.b_out_bps)));
    const list = document.createElement("ul");
    list.className = "topo-peer-list";
    for (const pair of link.ports) {
      const item = document.createElement("li");
      item.className = "topo-cable";
      const cable = cableText(pair.a_port, pair.b_port, "↔");
      if (pair.a_out_bps != null || pair.b_out_bps != null) {
        item.textContent = `${cable} · ${formatBps(pair.a_out_bps)} out / ${formatBps(pair.b_out_bps)} out`;
      } else {
        item.textContent = cable;
      }
      list.append(item);
    }
    panel.append(list);
  }

  /* ---------------------------------------------------------- page history */

  function currentNavSnap() {
    return {
      id: 0,
      name: state.report.name,
      title: state.report.title,
      filters: [...state.colFilters.entries()],
      params: [...state.reportParams.entries()],
      viewMode: state.viewMode,
    };
  }

  function navPageKey(snap) {
    const viewMode = ["bridge_domains", "services", "routers"].includes(snap.name)
      ? snap.viewMode
      : "";
    return JSON.stringify({ name: snap.name, filters: snap.filters, viewMode });
  }

  function applyNavSnap(snap) {
    state.colFilters.clear();
    for (const [column, pattern] of snap.filters || []) {
      if (pattern) state.colFilters.set(column, pattern);
    }
    if (snap.viewMode) state.viewMode = snap.viewMode;
    saveReportPreferences();
    if (snap.viewMode && state.report) {
      try {
        localStorage.setItem(`fcli-viewmode-${state.report.name}`, snap.viewMode);
      } catch (_err) {}
    }
  }

  function updateNavButtons() {
    if (!dom.navBack || !dom.navForward) return;
    const canBack = state.navIndex > 0;
    const canFwd = state.navIndex >= 0 && state.navIndex < state.navStack.length - 1;
    dom.navBack.disabled = !canBack;
    dom.navForward.disabled = !canFwd;
    const prev = canBack ? state.navStack[state.navIndex - 1] : null;
    const next = canFwd ? state.navStack[state.navIndex + 1] : null;
    dom.navBack.title = prev ? `Back to ${prev.title}` : "Back";
    dom.navForward.title = next ? `Forward to ${next.title}` : "Forward";
  }

  // The fragment names the page, and for a lens carries what it was asked.
  function pageUrl(snap) {
    const params = new URLSearchParams();
    for (const [name, value] of snap.params || []) params.set(name, value);
    const query = params.toString();
    return "#" + snap.name + (query ? "?" + query : "");
  }

  function syncCurrentVisit() {
    if (!state.report || state.navIndex < 0) return;
    const snap = currentNavSnap();
    snap.id = state.navStack[state.navIndex].id;
    state.navStack[state.navIndex] = snap;
    history.replaceState({ page: snap }, "", pageUrl(snap));
  }

  function recordVisit() {
    if (!state.report) return;
    const snap = currentNavSnap();
    const current = state.navStack[state.navIndex];
    if (current && navPageKey(current) === navPageKey(snap)) {
      snap.id = current.id;
      state.navStack[state.navIndex] = snap;
      history.replaceState({ page: snap }, "", pageUrl(snap));
      updateNavButtons();
      return;
    }
    snap.id = ++navSeq;
    const url = pageUrl(snap);
    if (state.navIndex < 0) {
      state.navStack = [snap];
      state.navIndex = 0;
      history.replaceState({ page: snap }, "", url);
    } else {
      state.navStack = state.navStack.slice(0, state.navIndex + 1);
      state.navStack.push(snap);
      state.navIndex = state.navStack.length - 1;
      history.pushState({ page: snap }, "", url);
    }
    updateNavButtons();
  }

  function restoreNavSnap(snap) {
    const report = state.reports.find((r) => r.name === snap.name);
    if (!report) return;
    if (state.report && state.report.name === snap.name) {
      applyNavSnap(snap);
      updateFilterUI();
      if (["bridge_domains", "services", "routers"].includes(snap.name)) {
        if (dom.viewModeBtn) {
          dom.viewModeBtn.hidden = false;
          dom.viewModeBtn.textContent =
            state.viewMode === "tree" ? "📊 Table View" : "🌲 Services View";
        }
        renderBody();
      } else {
        renderHead();
        renderBody();
      }
      updateNavButtons();
      return;
    }
    selectReport(report, { fromPop: true, snap });
    updateNavButtons();
  }

  function selectReport(report, { fromPop = false, snap = null } = {}) {
    if (state.report && !fromPop) {
      saveReportPreferences();
      syncCurrentVisit();
    }
    state.report = report;
    // A comparison belongs to the report it was made of.
    state.diff = null;
    state.snapshots = [];
    dom.diffBar.hidden = true;
    dom.compareMenu.hidden = true;
    state.columns = [];
    state.rows = [];
    state.errors = [];
    // Arguments belong to the report that declared them, and an LPM address
    // carried into another report would silently empty it.
    state.reportParams.clear();
    state.sort = { column: null, dir: 1 };
    state.previous.clear();
    state.identityColumn = null;
    state.firstPaint = true;
    state.windowSize = WINDOW_STEP;
    dom.title.textContent = report.title;
    dom.desc.textContent = report.description;
    if (dom.baselineBtn) dom.baselineBtn.hidden = report.name !== "changes";
    if (dom.ackAllBtn) {
      dom.ackAllBtn.hidden = report.name !== "incidents";
      dom.ackAllBtn.disabled = true; // until the incidents are in
      dom.ackAllBtn.textContent = "✓ ACK all";
    }
    if (dom.watchWrap) {
      dom.watchWrap.hidden = report.name !== "changes";
      dom.watchMenu.hidden = true;
      if (report.name === "changes") loadWatched();
    }
    dom.body.replaceChildren();
    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();
    dom.gridCols.replaceChildren();

    if (overviewTimer) {
      clearInterval(overviewTimer);
      overviewTimer = null;
    }
    if (topologyTimer) {
      clearInterval(topologyTimer);
      topologyTimer = null;
    }

    loadReportPreferences();
    if (snap) {
      state.pendingFilters = null;
      applyNavSnap(snap);
    } else {
      applyPendingFilters();
    }
    updateFilterUI();
    renderReportParams();
    renderReportList();

    if (isPanelReport(report.name)) {
      // A panel draws itself from its own endpoint instead of a table stream.
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = true;
      dom.pathGraphView.hidden = true;
      dom.viewModeBtn.hidden = true;
      dom.columnsBtn.hidden = true;
      dom.exportBtn.hidden = true;
      dom.compareBtn.hidden = true;
      dom.overviewDashboard.hidden = report.name !== "overview";
      dom.topologyView.hidden = report.name !== "topology";
      if (state.source) {
        state.source.close();
        state.source = null;
      }
      if (report.name === "overview") {
        loadOverview();
        overviewTimer = setInterval(loadOverview, 5000);
      } else {
        state.topoKey = "";
        state.topoSelection = null;
        loadTopology();
        topologyTimer = setInterval(loadTopology, 5000);
      }
    } else {
      dom.overviewDashboard.hidden = true;
      dom.topologyView.hidden = true;
      dom.columnsBtn.hidden = false;
      dom.exportBtn.hidden = false;
      // A lens answers a question asked now; there is nothing to keep and
      // compare a later answer against.
      dom.compareBtn.hidden = isLens(report);
      state.tree = null;
      state.records = null;
      state.graph = null;
      if (hasTreeView(report)) {
        const modes = viewModes(report);
        if (!(snap && snap.viewMode)) {
          state.viewMode = localStorage.getItem(`fcli-viewmode-${report.name}`) || modes[0];
        }
        if (!modes.includes(state.viewMode)) state.viewMode = modes[0];
        dom.viewModeBtn.hidden = false;
        dom.viewModeBtn.textContent = viewModeLabel();
      } else {
        dom.viewModeBtn.hidden = true;
      }
      dom.rowCount.textContent = "loading...";
      connect();
    }
    if (!fromPop) recordVisit();
  }

  function hasTreeView(report) {
    return Boolean(
      report && (isLens(report) || ["bridge_domains", "services", "routers"].includes(report.name))
    );
  }

  // The lens that walks a path is drawn as one; the other lenses and the
  // services pages fold into cards. The button names the view it switches to.
  function hasGraphView(report) {
    return isLens(report) && report.name === "path";
  }

  function viewModes(report) {
    return hasGraphView(report) ? ["graph", "tree", "table"] : ["tree", "table"];
  }

  function nextViewMode() {
    const modes = viewModes(state.report);
    return modes[(modes.indexOf(state.viewMode) + 1) % modes.length];
  }

  function viewModeLabel() {
    const next = nextViewMode();
    if (next === "table") return "📊 Table View";
    if (next === "graph") return "🗺 Path View";
    return isLens(state.report) ? "🌲 Tree View" : "🌲 Services View";
  }

  /* ------------------------------------------------------------- stream */

  function setLive(kind, label) {
    dom.liveDot.className = "dot " + kind;
    dom.liveLabel.textContent = label;
  }

  function connect() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    if (!state.report || isPanelReport(state.report.name) || state.paused) return;
    // A comparison is a verdict on two renderings; a stream pushing a third
    // over it would present as live something that is not.
    if (state.diff) return;
    // A lens has nothing to answer until it has been asked something.
    const missing = missingParams();
    if (missing.length) {
      setLive("", "waiting");
      state.columns = [];
      state.rows = [];
      state.tree = null;
      state.graph = null;
      dom.errors.hidden = true;
      dom.rowCount.textContent = `enter ${missing.map((spec) => spec.label.toLowerCase()).join(" and ")}`;
      renderBody();
      return;
    }
    const params = queryParams();
    params.set("refresh", dom.refresh.value);
    const source = new EventSource(
      `/api/stream/${encodeURIComponent(state.report.name)}?${params}`
    );
    state.source = source;
    setLive("live", "connecting");
    source.addEventListener("table", (event) => {
      setLive("live", "live");
      ingest(JSON.parse(event.data));
    });
    source.addEventListener("error", (event) => {
      if (event.data) {
        try {
          showErrors([{ node: "server", error: JSON.parse(event.data).error }]);
        } catch (_err) {
          /* non-JSON error payload */
        }
      }
      // A stream the server refused - a parameter it cannot use, a report it
      // does not offer - is closed for good rather than retried, and the
      // browser keeps the reason from us: ask for it once, the same way.
      if (source.readyState === EventSource.CLOSED) {
        setLive("error", "refused");
        explainRefusal(source, params);
        return;
      }
      setLive("error", "reconnecting");
    });
  }

  async function explainRefusal(source, params) {
    let reason = "the server refused the stream";
    try {
      const res = await fetch(`/api/report/${encodeURIComponent(state.report.name)}?${params}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        reason = body.error || `${reason} (HTTP ${res.status})`;
      }
    } catch (_err) {
      reason = "the server did not answer";
    }
    if (state.source !== source) return; // moved on meanwhile
    showErrors([{ node: "server", error: reason }]);
    dom.rowCount.textContent = "not loaded";
  }

  function ingest(table) {
    if (state.diff) return; // a stream that outlived the switch to a comparison
    const columnsChanged = table.columns.join(" ") !== state.columns.join(" ");
    state.columns = table.columns;
    state.rows = table.rows;
    state.tree = table.tree || null;
    state.records = table.records || null;
    if (state.report && state.report.name === "incidents") renderAckAll();
    state.graph = table.graph || null;
    state.errors = table.errors || [];
    if (columnsChanged) {
      state.identityColumn = null;
      renderHead();
      renderColumnsMenu();
    }
    showErrors(state.errors);
    dom.streamInfo.textContent = `${table.nodes} node(s), rendered in ${table.render_ms} ms`;
    dom.updated.textContent = "updated " + new Date().toLocaleTimeString();
    renderBody();
  }

  function showErrors(errors) {
    if (!errors || !errors.length) {
      dom.errors.hidden = true;
      dom.errors.replaceChildren();
      return;
    }
    dom.errors.hidden = false;
    dom.errors.replaceChildren(
      ...errors.map((entry) => {
        const line = document.createElement("div");
        line.textContent = `${entry.node}: ${entry.error}`;
        return line;
      })
    );
  }

  /* -------------------------------------------------------------- table */

  function visibleColumns() {
    return state.columns.filter((c) => !state.hidden.has(c));
  }

  function columnWidth(column) {
    return state.colWidths.get(column) || COL_WIDTH_DEFAULT;
  }

  // An empty cell closing every row, under the filler column below.
  function fillerCell(tag) {
    const cell = document.createElement(tag);
    cell.className = "col-filler-cell";
    cell.setAttribute("aria-hidden", "true");
    return cell;
  }

  function renderColGroup(columns) {
    const cols = columns.map((column) => {
      const col = document.createElement("col");
      col.style.width = `${columnWidth(column)}px`;
      col.dataset.column = column;
      return col;
    });
    // A trailing column with no width of its own, to soak up whatever space is
    // left over. A table narrower than its pane is stretched to fill it, and the
    // browser hands the slack back to the columns - which pins the last column's
    // right edge to the table's own and leaves its grip nothing to drag.
    const filler = document.createElement("col");
    filler.className = "col-filler";
    cols.push(filler);
    dom.gridCols.replaceChildren(...cols);
  }

  let columnResize = null;

  function startColumnResize(event, column) {
    event.preventDefault();
    event.stopPropagation();
    columnResize = {
      column,
      startX: event.clientX,
      startWidth: columnWidth(column),
    };
    document.body.classList.add("col-resizing");
    const onMove = (moveEvent) => {
      if (!columnResize) return;
      const delta = moveEvent.clientX - columnResize.startX;
      const width = Math.max(
        COL_WIDTH_MIN,
        Math.round(columnResize.startWidth + delta)
      );
      state.colWidths.set(columnResize.column, width);
      const col = dom.gridCols.querySelector(
        `col[data-column="${CSS.escape(columnResize.column)}"]`
      );
      if (col) col.style.width = `${width}px`;
    };
    const onUp = () => {
      columnResize = null;
      document.body.classList.remove("col-resizing");
      saveReportPreferences();
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
  }

  function resetColumnWidth(event, column) {
    event.preventDefault();
    event.stopPropagation();
    state.colWidths.delete(column);
    renderColGroup(visibleColumns());
    saveReportPreferences();
  }

  function renderHead() {
    const activeEl = document.activeElement;
    const activeColumn = activeEl && activeEl.dataset ? activeEl.dataset.column : null;
    const selStart = activeEl && typeof activeEl.selectionStart === "number" ? activeEl.selectionStart : null;
    const selEnd = activeEl && typeof activeEl.selectionEnd === "number" ? activeEl.selectionEnd : null;

    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();
    const columns = visibleColumns();
    renderColGroup(columns);
    for (const column of columns) {
      const th = document.createElement("th");
      const label = document.createElement("span");
      label.className = "col-label";
      label.textContent = column;
      if (state.colFilters.has(column)) {
        th.classList.add("filtered");
      }
      if (state.sort.column === column) {
        const arrow = document.createElement("span");
        arrow.className = "sort-arrow";
        arrow.textContent = state.sort.dir === 1 ? "▲" : "▼";
        label.append(arrow);
      }
      const grip = document.createElement("span");
      grip.className = "col-resize";
      grip.title = "Drag to resize · double-click to reset";
      grip.addEventListener("pointerdown", (event) => startColumnResize(event, column));
      grip.addEventListener("dblclick", (event) => resetColumnWidth(event, column));
      th.append(label, grip);
      th.addEventListener("click", (event) => {
        if (event.target.closest(".col-resize")) return;
        if (state.sort.column === column) {
          state.sort.dir = -state.sort.dir;
        } else {
          state.sort = { column, dir: 1 };
        }
        state.windowSize = WINDOW_STEP;
        renderHead();
        renderBody();
      });
      dom.headRow.append(th);

      const filterCell = document.createElement("th");
      const input = document.createElement("input");
      input.type = "search";
      input.placeholder = "filter";
      input.dataset.column = column;
      input.value = state.colFilters.get(column) || "";
      const showDefaultHint = () => {
        input.title =
          column === "st" && input.value === BGP_RIB_USED_FILTER
            ? "Only used routes (u). Clear to show all routes."
            : "";
      };
      showDefaultHint();
      input.addEventListener(
        "input",
        debounce(() => {
          showDefaultHint();
          const value = input.value.trim();
          if (value) state.colFilters.set(column, value);
          else state.colFilters.delete(column);
          state.windowSize = WINDOW_STEP;
          saveReportPreferences();
          updateFilterUI();
          th.classList.toggle("filtered", Boolean(value));
          renderBody();
        }, 150)
      );
      filterCell.append(input);
      dom.filterRow.append(filterCell);
    }

    dom.headRow.append(fillerCell("th"));
    dom.filterRow.append(fillerCell("th"));

    if (activeColumn) {
      const newInput = dom.filterRow.querySelector(`input[data-column="${CSS.escape(activeColumn)}"]`);
      if (newInput) {
        newInput.focus();
        if (selStart !== null && selEnd !== null) {
          try {
            newInput.setSelectionRange(selStart, selEnd);
          } catch (_err) {}
        }
      }
    }
  }

  function renderColumnsMenu() {
    dom.columnsMenu.replaceChildren();
    for (const column of state.columns) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !state.hidden.has(column);
      box.addEventListener("change", () => {
        if (box.checked) state.hidden.delete(column);
        else state.hidden.add(column);
        saveReportPreferences();
        renderHead();
        renderBody();
      });
      label.append(box, document.createTextNode(column));
      dom.columnsMenu.append(label);
    }
  }

  /* ------------------------------------------------------------ compare */

  // Two questions, one view: what changed since this fabric was working, and
  // why does this leaf not look like the one beside it. Both come back as an
  // ordinary table with a verdict column in front, so nothing here has to know
  // what report it is looking at.

  /** Whether the report on screen can be compared at all. */
  const isComparable = (report) =>
    Boolean(report) && !isPanelReport(report.name);

  function queryParams() {
    const params = new URLSearchParams();
    for (const [name, value] of state.reportParams) params.set(name, value);
    const inv = dom.invFilter.value.trim();
    if (inv) params.set("inv_filter", inv);
    return params;
  }

  async function loadSnapshots() {
    if (!state.report) return [];
    try {
      const res = await fetch(
        `/api/snapshots?report=${encodeURIComponent(state.report.name)}`
      );
      const data = await res.json();
      state.snapshots = data.snapshots || [];
    } catch (_err) {
      state.snapshots = [];
    }
    return state.snapshots;
  }

  function compareMenuButton(text, onClick, { title = "" } = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "menu-item";
    button.textContent = text;
    if (title) button.title = title;
    button.addEventListener("click", () => {
      dom.compareMenu.hidden = true;
      onClick();
    });
    return button;
  }

  function compareMenuHeading(text) {
    const heading = document.createElement("div");
    heading.className = "menu-heading";
    heading.textContent = text;
    return heading;
  }

  async function renderCompareMenu() {
    dom.compareMenu.replaceChildren();
    if (!isComparable(state.report)) {
      dom.compareMenu.append(compareMenuHeading("Nothing to compare here"));
      return;
    }

    dom.compareMenu.append(
      compareMenuButton("⛁ Save a snapshot of this table", saveSnapshot, {
        title: "Keep this table as it is now, to compare against later",
      })
    );

    const nodes = [...new Set(state.rows.map((row) => String(row.Node ?? "")))]
      .filter(Boolean)
      .sort(collator.compare);
    if (nodes.length >= 2) {
      dom.compareMenu.append(compareMenuHeading("Compare two nodes"));
      dom.compareMenu.append(nodePicker(nodes));
    }

    const snapshots = await loadSnapshots();
    dom.compareMenu.append(compareMenuHeading("Compare against a snapshot"));
    if (!snapshots.length) {
      const empty = document.createElement("div");
      empty.className = "menu-empty";
      empty.textContent = "none saved yet";
      dom.compareMenu.append(empty);
      return;
    }
    for (const snapshot of snapshots) {
      const row = document.createElement("div");
      row.className = "menu-row";
      // One directory holds the snapshots of every fabric, so say which one
      // this came from before it is clicked rather than only when refused.
      const elsewhere =
        snapshot.fabric && state.fabric && snapshot.fabric !== state.fabric;
      const button = compareMenuButton(
        `${snapshot.label} · ${snapshot.rows} row(s)`,
        () => showDiff({ against: snapshot.id }),
        {
          title: elsewhere
            ? `Taken of fabric '${snapshot.fabric}', not this one`
            : new Date(snapshot.taken_at * 1000).toLocaleString(),
        }
      );
      if (elsewhere) {
        button.classList.add("menu-item-foreign");
        const where = document.createElement("span");
        where.className = "menu-note";
        where.textContent = snapshot.fabric;
        button.append(where);
      }
      row.append(button);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "menu-remove";
      remove.textContent = "✕";
      remove.title = "Delete this snapshot";
      remove.addEventListener("click", async (event) => {
        event.stopPropagation();
        await fetch(`/api/snapshot/${encodeURIComponent(snapshot.id)}`, {
          method: "DELETE",
        });
        renderCompareMenu();
      });
      row.append(remove);
      dom.compareMenu.append(row);
    }
  }

  function nodePicker(nodes) {
    const wrap = document.createElement("div");
    wrap.className = "menu-row";
    const first = document.createElement("select");
    const second = document.createElement("select");
    for (const select of [first, second]) {
      select.className = "input";
      for (const node of nodes) {
        const option = document.createElement("option");
        option.value = node;
        option.textContent = node;
        select.append(option);
      }
    }
    first.value = nodes[0];
    second.value = nodes[1];
    const go = document.createElement("button");
    go.type = "button";
    go.className = "btn";
    go.textContent = "⇄";
    go.title = "Compare these two nodes";
    go.addEventListener("click", () => {
      dom.compareMenu.hidden = true;
      if (first.value === second.value) return;
      showDiff({ nodes: `${first.value},${second.value}` });
    });
    wrap.append(first, second, go);
    return wrap;
  }

  async function saveSnapshot() {
    const label = prompt(
      "Name this snapshot",
      `${state.report.title} ${new Date().toLocaleTimeString()}`
    );
    if (label === null) return;
    const params = queryParams();
    if (label.trim()) params.set("label", label.trim());
    const res = await fetch(
      `/api/snapshot/${encodeURIComponent(state.report.name)}?${params}`,
      { method: "POST" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showErrors([{ node: "snapshot", error: body.error || res.statusText }]);
      return;
    }
    renderCompareMenu();
  }

  /**
   * Put a comparison on screen in place of the live table.
   *
   * *how* is either ``{against: <snapshot id>}`` or ``{nodes: "a,b"}``, and is
   * remembered so the same comparison can be re-run when the unchanged rows
   * are toggled.
   */
  async function showDiff(how) {
    if (!isComparable(state.report)) return;
    const params = queryParams();
    if (how.against) params.set("against", how.against);
    if (how.nodes) params.set("nodes", how.nodes);
    if (dom.diffSame.checked) params.set("same", "1");

    const res = await fetch(
      `/api/diff/${encodeURIComponent(state.report.name)}?${params}`
    );
    const table = await res.json();
    if (!res.ok) {
      showErrors([{ node: "compare", error: table.error || res.statusText }]);
      return;
    }

    // The stream would push a live table over the comparison a second later.
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    setLive("idle", "comparing");

    state.diff = { ...how, ...(table.diff || {}) };
    // A comparison has columns of its own - the verdict, and no Node when two
    // nodes are what is being compared - so nothing is carried over.
    state.hidden.clear();
    state.sort = { column: null, dir: 1 };
    state.previous.clear();
    state.identityColumn = null;
    state.firstPaint = true;
    state.windowSize = WINDOW_STEP;
    state.columns = table.columns;
    state.rows = table.rows;
    state.errors = table.errors || [];

    renderDiffBar();
    showErrors(state.errors);
    renderHead();
    renderColumnsMenu();
    renderBody();
    dom.streamInfo.textContent = "comparison, not live";
    dom.updated.textContent = "compared " + new Date().toLocaleTimeString();
  }

  function renderDiffBar() {
    if (!state.diff) {
      dom.diffBar.hidden = true;
      return;
    }
    const { labels = [], counts = {}, keyed } = state.diff;
    dom.diffBar.hidden = false;
    dom.diffLabel.textContent = `${labels[0] ?? "before"} → ${labels[1] ?? "after"}`;
    const parts = [
      `${counts.removed ?? 0} gone`,
      `${counts.added ?? 0} new`,
    ];
    // Without key columns a change cannot be told from a row arriving and
    // another leaving, and saying "0 changed" would be a claim, not a count.
    if (keyed) parts.splice(1, 0, `${counts.changed ?? 0} changed`);
    parts.push(`${counts.same ?? 0} unchanged`);
    dom.diffCounts.textContent = parts.join(" · ");
    dom.diffCounts.title = keyed
      ? ""
      : "This report declares no key columns, so a changed row reads as one gone and one new";
  }

  /** Leave the comparison and go back to the live table. */
  function exitDiff({ reconnect = true } = {}) {
    if (!state.diff) return;
    state.diff = null;
    dom.diffBar.hidden = true;
    state.columns = [];
    state.rows = [];
    state.errors = [];
    state.hidden.clear();
    state.sort = { column: null, dir: 1 };
    state.previous.clear();
    state.identityColumn = null;
    state.firstPaint = true;
    dom.body.replaceChildren();
    dom.headRow.replaceChildren();
    dom.filterRow.replaceChildren();
    loadReportPreferences();
    showErrors([]);
    if (reconnect) {
      dom.rowCount.textContent = "loading...";
      connect();
    }
  }

  /** Rows left after the global search and the per-column filters. */
  function filteredRows() {
    const global = matcher(dom.globalSearch.value.trim());
    const columnMatchers = [...state.colFilters.entries()].map(([column, pattern]) => [
      column,
      matcher(pattern),
    ]);
    return state.rows.filter((row) => {
      for (const [column, test] of columnMatchers) {
        if (test && !test(String(row[column] ?? ""))) return false;
      }
      if (global) {
        const joined = visibleColumns()
          .map((c) => String(row[c] ?? ""))
          .join(" ");
        if (!global(joined)) return false;
      }
      return true;
    });
  }

  /**
   * Pick the column that identifies a row, so an update can be matched against
   * the previous render and only the cells that really changed are flashed.
   */
  function identityColumn(rows) {
    if (state.identityColumn !== null) return state.identityColumn;
    for (const column of state.columns.filter((c) => c !== "Node")) {
      const seen = new Set();
      let unique = true;
      for (const row of rows) {
        const key = `${row.Node} ${row[column]}`;
        if (seen.has(key)) {
          unique = false;
          break;
        }
        seen.add(key);
      }
      if (unique) {
        state.identityColumn = column;
        return column;
      }
    }
    state.identityColumn = "";
    return "";
  }

  const rowKey = (row, column) =>
    column ? `${row.Node} ${row[column]}` : state.columns.map((c) => row[c]).join(" ");

  function escapeRegex(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /** A column-filter pattern that matches any of *values* exactly. */
  function exactMatchPattern(values) {
    const unique = [...new Set((values || []).map((v) => String(v || "").trim()).filter(Boolean))];
    if (!unique.length) return "";
    if (unique.length === 1) return `^${escapeRegex(unique[0])}$`;
    return `^(${unique.map(escapeRegex).join("|")})$`;
  }

  function applyPendingFilters() {
    if (!state.pendingFilters) return;
    if (!state.report || state.report.name !== state.pendingFilters.report) return;
    state.colFilters.clear();
    for (const [column, pattern] of Object.entries(state.pendingFilters.filters || {})) {
      if (pattern) state.colFilters.set(column, pattern);
    }
    // A jump can ask the report something too, not only filter what it says.
    if (state.pendingFilters.params) {
      state.reportParams.clear();
      for (const [name, value] of Object.entries(state.pendingFilters.params)) {
        if (value) state.reportParams.set(name, value);
      }
    }
    state.pendingFilters = null;
    saveReportPreferences();
  }

  /** A column-filter pattern that matches any of *values* as a whole token.

  Used for network-instance names, which may share a cell with other VRFs
  (an IRB sits in both a mac-vrf and an ip-vrf).
  */
  function tokenMatchPattern(values) {
    const unique = [...new Set((values || []).map((v) => String(v || "").trim()).filter(Boolean))];
    if (!unique.length) return "";
    const parts = unique.map((v) => {
      const e = escapeRegex(v);
      // The NI cell may be a single name ("vrf1"), a comma list ("vrf1, mac-vrf-1"
      // for an IRB in both VRFs), or a JSON list leftover ("[\"vrf1\"]").
      return `(?:^|[,\\[\\]"'\\s])${e}(?=$|[,\\]\\]"'\\s])`;
    });
    return parts.length === 1 ? parts[0] : `(?:${parts.join("|")})`;
  }

  /** A next-hop cell may be a bare address, a comma list, or `addr/len (indirect)`. */
  function nextHopMatchPattern(ip) {
    const text = String(ip || "").trim();
    if (!text) return "";
    const e = escapeRegex(text);
    if (text.includes(":")) {
      return `(?:^|[^0-9a-f:])${e}(?=$|[^0-9a-f:])`;
    }
    return `(?:^|[^0-9])${e}(?=$|[^0-9])`;
  }

  function jumpToFilteredReport(reportName, niNames, nodeNames, extraFilters, params) {
    const filters = {};
    const ni = tokenMatchPattern(niNames);
    const nodes = exactMatchPattern(nodeNames);
    if (ni) filters.NI = ni;
    if (nodes) filters.Node = nodes;
    Object.assign(filters, extraFilters || {});
    state.pendingFilters = { report: reportName, filters, params: params || null };

    if (state.report && state.report.name === reportName) {
      syncCurrentVisit();
      applyPendingFilters();
      updateFilterUI();
      if (params) {
        renderReportParams();
        connect();
      }
      renderHead();
      renderBody();
      recordVisit();
      return;
    }
    const report = state.reports.find((r) => r.name === reportName);
    if (report) {
      selectReport(report);
    } else {
      state.pendingFilters = null;
    }
  }

  function makeReportJumpButton(label, title, reportName, niNames, nodeNames) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bd-report-jump-btn";
    button.textContent = label;
    button.title = title;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      jumpToFilteredReport(reportName, niNames, nodeNames);
    });
    return button;
  }

  function ribReportForAddress(ip) {
    const text = String(ip || "").trim();
    if (!text) return "";
    if (text.includes(":")) return "ipv6_rib";
    if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(text)) return "ipv4_rib";
    return "";
  }

  function jumpToNextHopRib(ip, niName) {
    const report = ribReportForAddress(ip);
    const nh = nextHopMatchPattern(ip);
    if (!report || !nh) return;
    jumpToFilteredReport(report, niName ? [niName] : [], [], { "next-hop": nh });
  }

  /**
   * The routes exchanged with a peer in one family, as the node that peers
   * with it holds them: *direction* ``received`` or ``advertised``.
   */
  function jumpToPeerRoutes(direction, nodeName, niName, peerAddress, family) {
    jumpToFilteredReport(`bgp_${direction}_routes`, [niName], [nodeName], {}, { peer: peerAddress, family });
  }

  /**
   * A BGP peers 'Rx/Act/Tx' cell with its Rx count linked to the routes the
   * peer sent and its Tx count to the ones sent to it, or null for a cell
   * that is not one. A count of 0 has no routes behind it and stays text.
   */
  function peerRoutesCell(row, column, value) {
    const peer = String(row.peer ?? "").trim();
    const family = (/^(\S+) Rx\/Act\/Tx$/.exec(column) || [])[1];
    const counts = /^(\d+)\/(\d+)\/(\d+)$/.exec(String(value));
    if (!peer || !row.Node || !family || !counts) return null;
    const [, rx, act, tx] = counts;
    const part = (count, direction, title) => {
      if (count === "0") return document.createTextNode(count);
      const link = document.createElement("a");
      link.className = "vrf-link";
      link.href = "#";
      link.textContent = count;
      link.title = title;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        jumpToPeerRoutes(direction, row.Node, row.NI, peer, family);
      });
      return link;
    };
    const cell = document.createDocumentFragment();
    cell.append(
      part(rx, "received", `Show the ${family} routes ${peer} sent to ${row.Node}`),
      document.createTextNode(`/${act}/`),
      part(tx, "advertised", `Show the ${family} routes ${row.Node} sent to ${peer}`)
    );
    return cell;
  }

  function jumpToBgpPeer(nodeName, niName, peerAddress) {
    jumpToFilteredReport("bgp_peers", [niName], [nodeName], {
      peer: exactMatchPattern([peerAddress]),
    });
  }

  // A virtual-ES label with its next-hop(s) turned into RIB jumps. The rest of
  // the label stays text; only `nh: <ip>` is a control, because that address
  // being active in this IP-VRF is what the segment tracks.
  function fillVirtualEsLabel(esPill, es, niName) {
    const match = /\bnh:\s*([^,]+)/.exec(es);
    if (!match) {
      esPill.textContent = es;
      return;
    }
    const ips = match[1].trim().split(/\s+/).filter(Boolean);
    esPill.append(document.createTextNode(es.slice(0, match.index) + "nh: "));
    ips.forEach((ip, index) => {
      if (index) esPill.append(document.createTextNode(" "));
      const report = ribReportForAddress(ip);
      if (!report) {
        esPill.append(document.createTextNode(ip));
        return;
      }
      const family = report === "ipv4_rib" ? "IPv4" : "IPv6";
      const link = document.createElement("a");
      link.className = "vrf-link";
      link.href = "#";
      link.textContent = ip;
      link.title = niName
        ? `Show ${family} routes in ${niName} with next-hop ${ip}`
        : `Show ${family} routes with next-hop ${ip}`;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        jumpToNextHopRib(ip, niName);
      });
      esPill.append(link);
    });
    esPill.append(document.createTextNode(es.slice(match.index + match[0].length)));
  }

  function reportJumpGroup(buttons) {
    const group = document.createElement("div");
    group.className = "bd-report-jump-group";
    group.append(...buttons);
    return group;
  }

  function jumpToVrf(targetType, vrfName, nodeName) {
    state.pendingJump = { targetType, vrfName, nodeName };
    const currentReportName = state.report ? state.report.name : "";
    const needSwitch = currentReportName !== "services" && currentReportName !== targetType;

    if (needSwitch) {
      const targetReport = state.reports.find((r) => r.name === targetType);
      if (targetReport) {
        selectReport(targetReport);
      }
    } else {
      executePendingJump();
    }
  }

  function executePendingJump() {
    if (!state.pendingJump) return;
    const { targetType, vrfName, nodeName } = state.pendingJump;

    let targetEl = null;
    if (targetType === "routers" || targetType === "ip-vrf") {
      if (nodeName) {
        targetEl = dom.servicesTreeView.querySelector(
          `[data-node="${CSS.escape(nodeName)}"] [data-ip-vrf="${CSS.escape(vrfName)}"]`
        );
      }
      if (!targetEl) {
        targetEl =
          dom.servicesTreeView.querySelector(`[data-ip-vrf="${CSS.escape(vrfName)}"]`) ||
          dom.servicesTreeView.querySelector(`[data-vrf-name="${CSS.escape(vrfName)}"]`);
      }
    } else {
      if (nodeName) {
        targetEl = dom.servicesTreeView.querySelector(
          `[data-node="${CSS.escape(nodeName)}"] [data-mac-vrf="${CSS.escape(vrfName)}"]`
        );
      }
      if (!targetEl) {
        targetEl =
          dom.servicesTreeView.querySelector(`[data-mac-vrf="${CSS.escape(vrfName)}"]`) ||
          dom.servicesTreeView.querySelector(`[data-vrf-name="${CSS.escape(vrfName)}"]`);
      }
    }

    if (targetEl) {
      // Ensure parent card is expanded
      const parentCard = targetEl.closest(".bd-card");
      if (parentCard && parentCard.classList.contains("is-collapsed")) {
        parentCard.classList.remove("is-collapsed");
        const body = parentCard.querySelector(".bd-body");
        if (body) body.hidden = false;
        const header = parentCard.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "true");
        const cardKey = parentCard.dataset.cardKey;
        if (cardKey) state.collapsedCards.delete(cardKey);
      }

      // Ensure parent node is expanded
      const parentNode = targetEl.closest(".bd-node");
      if (parentNode && parentNode.classList.contains("is-collapsed")) {
        parentNode.classList.remove("is-collapsed");
        const content = parentNode.querySelector(".bd-node-content");
        if (content) content.hidden = false;
        const title = parentNode.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "true");
        const nodeKey = parentNode.dataset.nodeKey;
        if (nodeKey) state.collapsedNodes.delete(nodeKey);
      }

      const parentVrf = targetEl.closest(".bd-vrf");
      if (parentVrf) {
        parentVrf.querySelectorAll(".bd-detail-section.is-collapsed").forEach((section) => {
          section.classList.remove("is-collapsed");
          const sectionContent = section.querySelector(".bd-detail-section-content");
          if (sectionContent) sectionContent.hidden = false;
          const sectionHeader = section.querySelector(".bd-detail-section-header");
          if (sectionHeader) sectionHeader.setAttribute("aria-expanded", "true");
          const sectionKey = section.dataset.sectionKey;
          if (sectionKey) state.collapsedSections.delete(sectionKey);
        });
      }

      targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
      targetEl.classList.remove("highlight-pulse");
      void targetEl.offsetWidth; // trigger reflow
      targetEl.classList.add("highlight-pulse");
      setTimeout(() => targetEl.classList.remove("highlight-pulse"), 3000);
      state.pendingJump = null;
    }
  }

  const UP_STATES = ["up", "enable", "enabled", "active", "established"];
  const DOWN_STATES = ["down", "disable", "disabled"];

  // Anything that is neither plainly up nor plainly down counts as degraded -
  // notably the "degraded" a service gets when only some of its interfaces are
  // up, which red would put on a par with a service that is entirely gone.
  // Standby is its own kind: an ethernet-segment holding a port down is intent,
  // and neither red nor orange is the truth about it.
  function stateKind(state) {
    const st = String(state || "").toLowerCase().trim();
    if (!st) return "";
    if (st === STANDBY_STATE) return "standby";
    if (UP_STATES.includes(st)) return "up";
    if (DOWN_STATES.includes(st)) return "down";
    return "degraded";
  }

  // The reports label an interface with its state, and a down one carries the
  // reason with it: "irb0.0 [down: net-inst-down]: 172.16.20.254/24".
  function labelledState(text) {
    const match = /\[([^\]]+)\]/.exec(text || "");
    return match ? stateKind(match[1].split(":")[0]) : "";
  }

  // *markUp* is off for pills that already carry a colour of their own to say
  // what kind of object they are; those only change colour when something is
  // wrong with them.
  function applyPillState(pill, text, { markUp = true } = {}) {
    const kind = labelledState(text);
    if (!kind || (kind === "up" && !markUp)) return;
    pill.classList.add(`pill-${kind}`);
  }

  function aggregateServiceState(rows) {
    if (!rows || !rows.length) {
      return { status: "unknown", className: "state-badge-down", badgeText: "UNKNOWN" };
    }
    const counts = { up: 0, down: 0, degraded: 0, standby: 0 };
    for (const row of rows) {
      counts[stateKind(row["Oper State"]) || "degraded"] += 1;
    }

    // A service standing by on one node is not a service in trouble.
    if (counts.up + counts.standby === rows.length) {
      return { status: "up", className: "state-badge-up", badgeText: "UP" };
    }
    if (counts.down === rows.length) {
      return { status: "down", className: "state-badge-down", badgeText: "DOWN" };
    }
    return { status: "degraded", className: "state-badge-warn", badgeText: "DEGRADED" };
  }

  function compareSubnets(a, b) {
    const v6a = a.includes(":") ? 1 : 0;
    const v6b = b.includes(":") ? 1 : 0;
    if (v6a !== v6b) return v6a - v6b;
    return a.localeCompare(b);
  }

  function unionSubnets(rows) {
    const seen = [];
    for (const r of rows) {
      if (!r["Subnets"]) continue;
      for (const s of String(r["Subnets"]).split(",").map((x) => x.trim())) {
        if (s && !seen.includes(s)) seen.push(s);
      }
    }
    return seen.sort(compareSubnets);
  }

  function subnetPills(subnets) {
    const el = document.createElement("div");
    el.className = "bd-header-subnets";
    for (const s of subnets) {
      const pill = document.createElement("span");
      pill.className = "bd-subnet-pill";
      pill.textContent = s;
      el.append(pill);
    }
    return el;
  }

  function isGatewayRow(row) {
    const value = row && row["Gateway"];
    return value === "Y" || value === true || value === "true";
  }

  function isDciService(rows) {
    return Boolean(rows && rows.length && rows.every(isGatewayRow));
  }

  function roleBadge(text, className) {
    const el = document.createElement("span");
    el.className = className;
    el.textContent = text;
    return el;
  }

  function nodeNameWithIps(nodeName, nodeRows) {
    const wrap = document.createElement("span");
    wrap.className = "bd-node-name";

    const name = document.createElement("span");
    name.textContent = `🖥️ Node: ${nodeName}`;
    wrap.append(name);

    const isGateway = (nodeRows || []).some(isGatewayRow);
    if (isGateway) wrap.append(roleBadge("Gateway", "bd-gateway-badge"));

    const row = (nodeRows && nodeRows[0]) || {};
    const ips = [row["System IPv4"], row["System IPv6"]]
      .map((v) => (typeof v === "string" ? v.trim() : ""))
      .filter(Boolean);
    if (ips.length) {
      const ipSpan = document.createElement("span");
      ipSpan.className = "bd-node-ips";
      ipSpan.textContent = ips.join("  ·  ");
      wrap.append(ipSpan);
    }
    return wrap;
  }

  // A network-instance covered by another report, as a button that jumps to it -
  // the association is worth a control of its own rather than a link buried in
  // the text of a pill.
  function makeJumpButton(report, target, node, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pill pill-jump";
    button.textContent = target;
    button.title = title;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      jumpToVrf(report, target, node);
    });
    return button;
  }

  function makeCollapsibleDetailSection(sectionKey, labelText, contentEl) {
    const isCollapsed = state.collapsedSections.has(sectionKey);

    const section = document.createElement("div");
    section.className = "bd-detail-section";
    section.dataset.sectionKey = sectionKey;
    if (isCollapsed) section.classList.add("is-collapsed");

    const header = document.createElement("div");
    header.className = "bd-detail-section-header";
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");
    header.setAttribute("aria-expanded", isCollapsed ? "false" : "true");

    const chevron = document.createElement("span");
    chevron.className = "bd-detail-section-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "▼";

    const label = document.createElement("strong");
    label.className = "bd-detail-label";
    label.textContent = labelText;

    header.append(chevron, label);

    const content = document.createElement("div");
    content.className = "bd-detail-section-content";
    if (isCollapsed) content.hidden = true;
    content.append(contentEl);

    const toggle = () => {
      const collapsed = section.classList.toggle("is-collapsed");
      content.hidden = collapsed;
      header.setAttribute("aria-expanded", collapsed ? "false" : "true");
      if (collapsed) {
        state.collapsedSections.add(sectionKey);
      } else {
        state.collapsedSections.delete(sectionKey);
      }
    };

    header.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      toggle();
    });

    header.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    section.append(header, content);
    return section;
  }

  /** Open or close every node block and detail section inside one card. */
  function setCardContentsCollapsed(card, collapsed) {
    card.querySelectorAll(".bd-node").forEach((node) => {
      node.classList.toggle("is-collapsed", collapsed);
      const content = node.querySelector(".bd-node-content");
      if (content) content.hidden = collapsed;
      const title = node.querySelector(".bd-node-title");
      if (title) title.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const nodeKey = node.dataset.nodeKey;
      if (!nodeKey) return;
      if (collapsed) state.collapsedNodes.add(nodeKey);
      else state.collapsedNodes.delete(nodeKey);
    });
    card.querySelectorAll(".bd-detail-section").forEach((section) => {
      section.classList.toggle("is-collapsed", collapsed);
      const content = section.querySelector(".bd-detail-section-content");
      if (content) content.hidden = collapsed;
      const header = section.querySelector(".bd-detail-section-header");
      if (header) header.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const sectionKey = section.dataset.sectionKey;
      if (!sectionKey) return;
      if (collapsed) state.collapsedSections.add(sectionKey);
      else state.collapsedSections.delete(sectionKey);
    });
  }

  // Expand/collapse for the levels *inside* one card. The card header already
  // toggles the card itself, so these reach the per-node blocks and their
  // fields, which is where a router spanning several nodes gets too deep to
  // scan. Collapsing leaves the card open, so the result stays visible.
  function cardScopeControls(card, expandCard) {
    const group = document.createElement("div");
    group.className = "bd-card-controls";

    const makeButton = (label, title, collapsed) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "bd-card-control-btn";
      button.textContent = label;
      button.title = title;
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        expandCard();
        setCardContentsCollapsed(card, collapsed);
      });
      return button;
    };

    group.append(
      makeButton("⊞", "Expand every node and field in this card", false),
      makeButton("⊟", "Collapse every node and field in this card", true),
    );
    return group;
  }

  function renderBridgeDomainsCards(rows) {
    const bdMap = new Map();
    for (const row of rows) {
      const bdName = row["Bridge Domain"] || row["Route Targets"] || "unassigned";
      const site = row["Site"] ? String(row["Site"]) : "";
      const groupKey = site ? `${bdName}@@${site}` : bdName;
      if (!bdMap.has(groupKey)) bdMap.set(groupKey, []);
      bdMap.get(groupKey).push(row);
    }

    for (const [groupKey, bdRows] of bdMap) {
      const bdName = (bdRows[0] && (bdRows[0]["Bridge Domain"] || bdRows[0]["Route Targets"])) || groupKey.split("@@")[0];
      const site = (bdRows[0] && bdRows[0]["Site"]) ? String(bdRows[0]["Site"]) : "";
      const siteSuffix = site ? ` (${site})` : "";
      const cardKey = `bd:${groupKey}`;
      const isCardCollapsed = state.collapsedCards.has(cardKey);
      const cardAgg = aggregateServiceState(bdRows);

      const card = document.createElement("div");
      card.className = `bd-card bd-state-${cardAgg.status}`;
      card.dataset.cardKey = cardKey;
      if (isCardCollapsed) card.classList.add("is-collapsed");

      const macVrfFirstName = bdRows.map((r) => r["MAC-VRF"]).find(Boolean) || "";
      if (macVrfFirstName) card.dataset.vrfName = macVrfFirstName;

      // Union of MAC-VRF names in this Bridge Domain
      const vrfNames = [...new Set(bdRows.map((r) => r["MAC-VRF"]).filter(Boolean))].sort();
      const hasVrfMismatch = vrfNames.length > 1;

      // Union of Subnets across child nodes in this Bridge Domain
      const allSubnets = [];
      for (const r of bdRows) {
        if (r["Subnets"]) {
          r["Subnets"].split(",").map((s) => s.trim()).forEach((s) => {
            if (s && !allSubnets.includes(s)) allSubnets.push(s);
          });
        }
      }
      const subnetsStr = allSubnets.length ? ` (${allSubnets.join(", ")})` : "";

      const header = document.createElement("div");
      header.className = "bd-header";
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-expanded", isCardCollapsed ? "false" : "true");

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const chevron = document.createElement("span");
      chevron.className = "bd-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "▼";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🌉";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Bridge-domain: ${vrfTitleStr}${siteSuffix}${subnetsStr}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(bdRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title);
      if (isDciService(bdRows)) topRow.append(roleBadge("DCI", "bd-dci-badge"));
      topRow.append(stateBadge, badge);
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      rtLabel.textContent = `Route-target: ${bdName}`;
      subRow.append(rtLabel);

      if (hasVrfMismatch) {
        const mismatchBadge = document.createElement("span");
        mismatchBadge.className = "bd-mismatch-badge";
        mismatchBadge.textContent = `⚠️ Mismatched VRF Names (${vrfNames.join(" vs ")})`;
        subRow.append(mismatchBadge);
      }

      const nodeNames = [...new Set(bdRows.map((r) => r.Node).filter(Boolean))];
      const niHint = vrfNames.length ? vrfNames.join(", ") : "this bridge domain";
      const nodeHint = nodeNames.length ? nodeNames.join(", ") : "participating nodes";
      subRow.append(
        reportJumpGroup([
          makeReportJumpButton(
            "Bridge table",
            `MAC table for ${niHint} on ${nodeHint}`,
            "mac",
            vrfNames,
            nodeNames,
          ),
        ]),
      );

      header.append(subRow);
      card.append(header);

      const body = document.createElement("div");
      body.className = "bd-body";
      if (isCardCollapsed) body.hidden = true;

      const toggleCard = () => {
        const collapsed = card.classList.toggle("is-collapsed");
        body.hidden = collapsed;
        header.setAttribute("aria-expanded", collapsed ? "false" : "true");
        if (collapsed) {
          state.collapsedCards.add(cardKey);
        } else {
          state.collapsedCards.delete(cardKey);
        }
      };

      header.addEventListener("click", (e) => {
        if (e.target.closest("a, button")) return;
        toggleCard();
      });

      header.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest("a, button")) return;
          e.preventDefault();
          toggleCard();
        }
      });

      const nodeMap = new Map();
      for (const row of bdRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeKey = `${cardKey}:node:${nodeName}`;
        const isNodeCollapsed = state.collapsedNodes.has(nodeKey);

        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;
        nodeDiv.dataset.nodeKey = nodeKey;
        if (isNodeCollapsed) nodeDiv.classList.add("is-collapsed");

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.setAttribute("role", "button");
        nodeTitle.setAttribute("tabindex", "0");
        nodeTitle.setAttribute("aria-expanded", isNodeCollapsed ? "false" : "true");

        const nodeChevron = document.createElement("span");
        nodeChevron.className = "bd-node-chevron";
        nodeChevron.setAttribute("aria-hidden", "true");
        nodeChevron.textContent = "▼";

        const nodeText = nodeNameWithIps(nodeName, nodeRows);

        const nodeAgg = aggregateServiceState(nodeRows);

        const nodeStateBadge = document.createElement("span");
        nodeStateBadge.className = `bd-node-state ${nodeAgg.className}`;
        nodeStateBadge.textContent = nodeAgg.badgeText;

        const nodeBadge = document.createElement("span");
        nodeBadge.className = "bd-node-count";
        nodeBadge.textContent = `${nodeRows.length} item${nodeRows.length === 1 ? "" : "s"}`;

        nodeTitle.append(nodeChevron, nodeText, nodeStateBadge, nodeBadge);
        nodeDiv.append(nodeTitle);

        const nodeContent = document.createElement("div");
        nodeContent.className = "bd-node-content";
        if (isNodeCollapsed) nodeContent.hidden = true;

        const toggleNode = () => {
          const collapsed = nodeDiv.classList.toggle("is-collapsed");
          nodeContent.hidden = collapsed;
          nodeTitle.setAttribute("aria-expanded", collapsed ? "false" : "true");
          if (collapsed) {
            state.collapsedNodes.add(nodeKey);
          } else {
            state.collapsedNodes.delete(nodeKey);
          }
        };

        nodeTitle.addEventListener("click", (e) => {
          if (e.target.closest("a, button")) return;
          toggleNode();
        });

        nodeTitle.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            if (e.target.closest("a, button")) return;
            e.preventDefault();
            toggleNode();
          }
        });

        for (const row of nodeRows) {
          const vrfDiv = document.createElement("div");
          vrfDiv.className = "bd-vrf";
          if (row["MAC-VRF"]) vrfDiv.dataset.macVrf = row["MAC-VRF"];

          const vrfHeader = document.createElement("div");
          vrfHeader.className = "bd-vrf-header";

          const vrfName = document.createElement("span");
          const subnetsStr = row["Subnets"] ? ` (${row["Subnets"]})` : "";
          vrfName.textContent = `📦 MAC-VRF: ${row["MAC-VRF"] || "-"}${subnetsStr}`;

          const vrfTitle = document.createElement("span");
          vrfTitle.className = "bd-vrf-title";
          vrfTitle.append(vrfName);
          if (row["BGP Instance"]) {
            const instBadge = document.createElement("span");
            instBadge.className = "bd-bgp-inst-badge";
            instBadge.textContent = `bgp-instance ${row["BGP Instance"]}`;
            vrfTitle.append(instBadge);
          }

          const stateSpan = document.createElement("span");
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;

          vrfHeader.append(vrfTitle, stateSpan);
          vrfDiv.append(vrfHeader);

          const details = document.createElement("div");
          details.className = "bd-details";

          // 1. IRB interface (max 1; its ip-vrf, if any, gets its own button)
          const irbStr = row["IRB Interface"] || "-";
          if (irbStr !== "-") {
            const irbRowDiv = document.createElement("div");
            irbRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "IRB interface:";
            irbRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            // Split only where a new IRB starts, so the commas inside an address
            // list or a list of VRFs stay where they belong.
            irbStr.split(/,\s*(?=irb)/i).forEach((entry) => {
              const [itfText, ...vrfText] = entry.split("->");

              const p = document.createElement("span");
              p.className = "pill";
              applyPillState(p, itfText);
              p.textContent = itfText.trim();
              pillGroup.append(p);

              const vrfs = vrfText
                .join("->")
                .split(",")
                .map((v) => v.trim())
                .filter(Boolean);
              vrfs.forEach((vrf) => {
                const arrow = document.createElement("span");
                arrow.className = "pill-arrow";
                arrow.textContent = "→";
                pillGroup.append(
                  arrow,
                  makeJumpButton("routers", vrf, nodeName, `Jump to Router ${vrf}`),
                );
              });
            });
            irbRowDiv.append(pillGroup);
            details.append(irbRowDiv);
          }

          // 2. bridge sub-interfaces, each with the ethernet-segment its
          //    parent port sits on
          const subitfStr = row["Sub-Interfaces"] || "-";
          if (subitfStr !== "-") {
            const subRowDiv = document.createElement("div");
            subRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "bridge sub-interfaces:";
            subRowDiv.append(label);

            const lines = document.createElement("div");
            lines.className = "pill-lines";
            // A line each, so a member and its segment stay side by side:
            // pairing them up across two lists is what made a bridge domain
            // with several multi-homed members unreadable.
            subitfStr.split(";").forEach((entry) => {
              const [itfText, ...esText] = entry.split("->");

              const line = document.createElement("div");
              line.className = "pill-group";

              const p = document.createElement("span");
              p.className = "pill";
              applyPillState(p, itfText);
              p.textContent = itfText.trim();
              line.append(p);

              const es = esText.join("->").trim();
              if (es) {
                const arrow = document.createElement("span");
                arrow.className = "pill-arrow";
                arrow.textContent = "→";

                const esPill = document.createElement("span");
                esPill.className = "pill pill-es";
                const oper = /\boper:\s*(\S+)/.exec(es);
                const kind = oper ? stateKind(oper[1]) : "";
                if (kind && kind !== "up") esPill.classList.add(`pill-${kind}`);
                esPill.textContent = es;
                line.append(arrow, esPill);
              }
              lines.append(line);
            });
            subRowDiv.append(lines);
            details.append(subRowDiv);
          }

          // 3. vxlan-interface
          const vxlanStr = row["VXLAN Interface"] || "-";
          if (vxlanStr !== "-") {
            const vxRowDiv = document.createElement("div");
            vxRowDiv.className = "bd-detail-row";

            const label = document.createElement("strong");
            label.className = "bd-detail-label";
            label.textContent = "vxlan-interface:";
            vxRowDiv.append(label);

            const pillGroup = document.createElement("div");
            pillGroup.className = "pill-group";
            vxlanStr.split(",").forEach((v) => {
              const p = document.createElement("span");
              p.className = "pill pill-vxlan";
              p.textContent = v.trim();
              pillGroup.append(p);
            });
            vxRowDiv.append(pillGroup);
            details.append(vxRowDiv);
          }

          vrfDiv.append(details);
          nodeContent.append(vrfDiv);
        }
        nodeDiv.append(nodeContent);
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderRoutersCards(rows) {
    const routerMap = new Map();
    for (const row of rows) {
      if (row["IP-VRF"] === "mgmt") continue;
      const routerName = row["Router"] || row["Route Targets"] || "unassigned";
      const site = row["Site"] ? String(row["Site"]) : "";
      const groupKey = site ? `${routerName}@@${site}` : routerName;
      if (!routerMap.has(groupKey)) routerMap.set(groupKey, []);
      routerMap.get(groupKey).push(row);
    }

    for (const [groupKey, rRows] of routerMap) {
      const routerName = (rRows[0] && (rRows[0]["Router"] || rRows[0]["Route Targets"])) || groupKey.split("@@")[0];
      const site = (rRows[0] && rRows[0]["Site"]) ? String(rRows[0]["Site"]) : "";
      const siteSuffix = site ? ` (${site})` : "";
      const cardKey = `router:${groupKey}`;
      const isCardCollapsed = state.collapsedCards.has(cardKey);
      const cardAgg = aggregateServiceState(rRows);

      const card = document.createElement("div");
      card.className = `bd-card bd-state-${cardAgg.status}`;
      card.dataset.cardKey = cardKey;
      if (isCardCollapsed) card.classList.add("is-collapsed");

      const ipVrfFirstName = rRows.map((r) => r["IP-VRF"]).find(Boolean) || "";
      if (ipVrfFirstName) card.dataset.vrfName = ipVrfFirstName;

      // Union of IP-VRF names in this Router
      const vrfNames = [...new Set(rRows.map((r) => r["IP-VRF"]).filter(Boolean))].sort();
      const hasVrfMismatch = vrfNames.length > 1;

      const header = document.createElement("div");
      header.className = "bd-header";
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-expanded", isCardCollapsed ? "false" : "true");

      const topRow = document.createElement("div");
      topRow.className = "bd-header-top";

      const chevron = document.createElement("span");
      chevron.className = "bd-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "▼";

      const icon = document.createElement("span");
      icon.className = "bd-icon";
      icon.textContent = "🔀";

      const title = document.createElement("span");
      title.className = "bd-title";
      const vrfTitleStr = vrfNames.join(", ") + (hasVrfMismatch ? " (!)" : "");
      title.textContent = `Router: ${vrfTitleStr}${siteSuffix}`;

      const stateBadge = document.createElement("span");
      stateBadge.className = `bd-state-badge ${cardAgg.className}`;
      stateBadge.textContent = cardAgg.badgeText;

      const nodesCount = new Set(rRows.map((r) => r.Node)).size;
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = `${nodesCount} Node${nodesCount === 1 ? "" : "s"}`;

      topRow.append(chevron, icon, title);
      if (isDciService(rRows)) topRow.append(roleBadge("DCI", "bd-dci-badge"));
      // ``expandCard`` is only read when a button is clicked, by which time the
      // card is built and the binding below has been evaluated.
      topRow.append(
        stateBadge,
        badge,
        cardScopeControls(card, () => expandCard()),
      );
      header.append(topRow);

      const subRow = document.createElement("div");
      subRow.className = "bd-header-sub";

      const rtBlock = document.createElement("div");
      rtBlock.className = "bd-rt-block";

      const rtLabel = document.createElement("span");
      rtLabel.className = "bd-rt-label";
      const isIsolated = !routerName || routerName === "none (isolated)" || routerName.startsWith("none (isolated)") || routerName.startsWith("ip-vrf:") || routerName === "unassigned";
      rtLabel.textContent = isIsolated ? "Route-target: none (isolated)" : `Route-target: ${routerName}`;
      rtBlock.append(rtLabel);

      const allSubnets = unionSubnets(rRows);
      if (allSubnets.length) rtBlock.append(subnetPills(allSubnets));
      subRow.append(rtBlock);

      if (hasVrfMismatch) {
        const mismatchBadge = document.createElement("span");
        mismatchBadge.className = "bd-mismatch-badge";
        mismatchBadge.textContent = `⚠️ Mismatched VRF Names (${vrfNames.join(" vs ")})`;
        subRow.append(mismatchBadge);
      }

      const nodeNames = [...new Set(rRows.map((r) => r.Node).filter(Boolean))];
      const niHint = vrfNames.length ? vrfNames.join(", ") : "this router";
      const nodeHint = nodeNames.length ? nodeNames.join(", ") : "participating nodes";
      subRow.append(
        reportJumpGroup([
          makeReportJumpButton(
            "IPv4 RIB",
            `IPv4 routes for ${niHint} on ${nodeHint}`,
            "ipv4_rib",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "IPv6 RIB",
            `IPv6 routes for ${niHint} on ${nodeHint}`,
            "ipv6_rib",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "ARP",
            `ARP table for ${niHint} on ${nodeHint}`,
            "arp",
            vrfNames,
            nodeNames,
          ),
          makeReportJumpButton(
            "ND",
            `IPv6 neighbors for ${niHint} on ${nodeHint}`,
            "nd",
            vrfNames,
            nodeNames,
          ),
        ]),
      );

      header.append(subRow);
      card.append(header);

      const body = document.createElement("div");
      body.className = "bd-body";
      if (isCardCollapsed) body.hidden = true;

      const toggleCard = () => {
        const collapsed = card.classList.toggle("is-collapsed");
        body.hidden = collapsed;
        header.setAttribute("aria-expanded", collapsed ? "false" : "true");
        if (collapsed) {
          state.collapsedCards.add(cardKey);
        } else {
          state.collapsedCards.delete(cardKey);
        }
      };

      header.addEventListener("click", (e) => {
        if (e.target.closest("a, button")) return;
        toggleCard();
      });

      header.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest("a, button")) return;
          e.preventDefault();
          toggleCard();
        }
      });

      const expandCard = () => {
        if (card.classList.contains("is-collapsed")) toggleCard();
      };

      const nodeMap = new Map();
      for (const row of rRows) {
        const nodeName = row.Node || "unknown";
        if (!nodeMap.has(nodeName)) nodeMap.set(nodeName, []);
        nodeMap.get(nodeName).push(row);
      }

      for (const [nodeName, nodeRows] of nodeMap) {
        const nodeKey = `${cardKey}:node:${nodeName}`;
        const isNodeCollapsed = state.collapsedNodes.has(nodeKey);

        const nodeDiv = document.createElement("div");
        nodeDiv.className = "bd-node";
        nodeDiv.dataset.node = nodeName;
        nodeDiv.dataset.nodeKey = nodeKey;
        if (isNodeCollapsed) nodeDiv.classList.add("is-collapsed");

        const nodeTitle = document.createElement("div");
        nodeTitle.className = "bd-node-title";
        nodeTitle.setAttribute("role", "button");
        nodeTitle.setAttribute("tabindex", "0");
        nodeTitle.setAttribute("aria-expanded", isNodeCollapsed ? "false" : "true");

        const nodeChevron = document.createElement("span");
        nodeChevron.className = "bd-node-chevron";
        nodeChevron.setAttribute("aria-hidden", "true");
        nodeChevron.textContent = "▼";

        const nodeText = nodeNameWithIps(nodeName, nodeRows);

        const nodeAgg = aggregateServiceState(nodeRows);

        const nodeStateBadge = document.createElement("span");
        nodeStateBadge.className = `bd-node-state ${nodeAgg.className}`;
        nodeStateBadge.textContent = nodeAgg.badgeText;

        const nodeBadge = document.createElement("span");
        nodeBadge.className = "bd-node-count";
        nodeBadge.textContent = `${nodeRows.length} item${nodeRows.length === 1 ? "" : "s"}`;

        nodeTitle.append(nodeChevron, nodeText, nodeStateBadge, nodeBadge);
        nodeDiv.append(nodeTitle);

        const nodeContent = document.createElement("div");
        nodeContent.className = "bd-node-content";
        if (isNodeCollapsed) nodeContent.hidden = true;

        const toggleNode = () => {
          const collapsed = nodeDiv.classList.toggle("is-collapsed");
          nodeContent.hidden = collapsed;
          nodeTitle.setAttribute("aria-expanded", collapsed ? "false" : "true");
          if (collapsed) {
            state.collapsedNodes.add(nodeKey);
          } else {
            state.collapsedNodes.delete(nodeKey);
          }
        };

        nodeTitle.addEventListener("click", (e) => {
          if (e.target.closest("a, button")) return;
          toggleNode();
        });

        nodeTitle.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            if (e.target.closest("a, button")) return;
            e.preventDefault();
            toggleNode();
          }
        });

        for (const row of nodeRows) {
          const vrfDiv = document.createElement("div");
          vrfDiv.className = "bd-vrf";
          if (row["IP-VRF"]) vrfDiv.dataset.ipVrf = row["IP-VRF"];

          const vrfHeader = document.createElement("div");
          vrfHeader.className = "bd-vrf-header";

          const vrfName = document.createElement("span");
          vrfName.textContent = `📦 IP-VRF: ${row["IP-VRF"] || "-"}`;

          const vrfTitle = document.createElement("span");
          vrfTitle.className = "bd-vrf-title";
          vrfTitle.append(vrfName);

          const stateSpan = document.createElement("span");
          const instanceAgg = aggregateServiceState([row]);
          stateSpan.className = instanceAgg.className;
          stateSpan.textContent = row["Oper State"] || instanceAgg.badgeText;
          vrfTitle.append(stateSpan);

          if (row["BGP Instance"]) {
            const instBadge = document.createElement("span");
            instBadge.className = "bd-bgp-inst-badge";
            instBadge.textContent = `bgp-instance ${row["BGP Instance"]}`;
            vrfTitle.append(instBadge);
          }
          if (row["EVI"]) {
            const eviBadge = document.createElement("span");
            eviBadge.className = "bd-bgp-inst-badge";
            eviBadge.textContent = `evi ${row["EVI"]}`;
            vrfTitle.append(eviBadge);
          }

          vrfHeader.append(vrfTitle);
          vrfDiv.append(vrfHeader);

          const details = document.createElement("div");
          details.className = "bd-details";
          const ipVrfName = row["IP-VRF"] || "-";
          const sectionPrefix = `${nodeKey}:${ipVrfName}`;

          // 1. MAC-VRF's
          const macVrfsStr = row["MAC-VRFs"] || "-";
          if (macVrfsStr !== "-") {
            const lines = document.createElement("div");
            lines.className = "pill-lines";
            const items = macVrfsStr.split(/,\s*(?=[^\s(]+\s*\()/g);
            items.forEach((itemStr) => {
              const line = document.createElement("div");
              line.className = "pill-group";

              const p = document.createElement("span");
              p.className = "pill pill-macvrf";
              applyPillState(p, itemStr);

              const match = itemStr.trim().match(/^([^\s(]+)(.*)$/);
              if (match) {
                const macName = match[1];
                const restText = match[2];

                const link = document.createElement("a");
                link.className = "vrf-link";
                link.textContent = macName;
                link.href = "#";
                link.title = `Jump to Bridge Domain ${macName}`;
                link.addEventListener("click", (e) => {
                  e.preventDefault();
                  jumpToVrf("bridge_domains", macName, nodeName);
                });

                p.append(link, document.createTextNode(restText));
              } else {
                p.textContent = itemStr.trim();
              }
              line.append(p);
              lines.append(line);
            });
            details.append(
              makeCollapsibleDetailSection(`${sectionPrefix}:mac-vrfs`, "MAC-VRF's:", lines),
            );
          }

          // 2. Routed interfaces
          const routedStr = row["Routed Interfaces"] || "-";
          if (routedStr !== "-") {
            const lines = document.createElement("div");
            lines.className = "pill-lines";
            // Split only where a new interface starts - the state label is what
            // says one does. An interface addressed in both families lists its
            // addresses with a comma between them, and splitting on those left
            // the second address as a pill of its own, with no state to colour.
            routedStr.split(/,\s*(?=[^\s,]+\s\[)/).forEach((s) => {
              const line = document.createElement("div");
              line.className = "pill-group";

              const p = document.createElement("span");
              p.className = "pill";
              applyPillState(p, s);
              p.textContent = s.trim();
              line.append(p);
              lines.append(line);
            });
            details.append(
              makeCollapsibleDetailSection(
                `${sectionPrefix}:routed-interfaces`,
                "Routed interfaces:",
                lines,
              ),
            );
          }

          // 3. BGP peers
          const bgpPeersStr = row["BGP Peers"] || "-";
          if (bgpPeersStr !== "-") {
            const lines = document.createElement("div");
            lines.className = "pill-lines";
            bgpPeersStr.split(/,\s*/).forEach((itemStr) => {
              const match = itemStr.trim().match(/^(\S+)\s->\s(\S+)\s+(UP|DOWN)$/);
              if (!match) return;
              const [, localAddr, peerAddr, peerState] = match;

              const line = document.createElement("div");
              line.className = "pill-group";

              const localPill = document.createElement("span");
              localPill.className = "pill";
              localPill.textContent = localAddr;

              const arrow = document.createElement("span");
              arrow.className = "pill-arrow";
              arrow.textContent = "→";

              const peerPill = document.createElement("span");
              peerPill.className = "pill";
              const kind = stateKind(peerState);
              if (kind) peerPill.classList.add(`pill-${kind}`);

              const link = document.createElement("a");
              link.className = "vrf-link";
              link.textContent = peerAddr;
              link.href = "#";
              link.title = `Show BGP peer ${peerAddr} in ${row["IP-VRF"]} on ${nodeName}`;
              link.addEventListener("click", (e) => {
                e.preventDefault();
                e.stopPropagation();
                jumpToBgpPeer(nodeName, row["IP-VRF"], peerAddr);
              });

              peerPill.append(link, document.createTextNode(` ${peerState}`));
              line.append(localPill, arrow, peerPill);
              lines.append(line);
            });
            details.append(
              makeCollapsibleDetailSection(`${sectionPrefix}:bgp-peers`, "BGP peers:", lines),
            );
          }

          // 4. Virtual ethernet-segments, matched to this router on its EVI
          const vesStr = row["Virtual ES"] || "-";
          if (vesStr !== "-") {
            const lines = document.createElement("div");
            lines.className = "pill-lines";
            vesStr.split(";").forEach((entry) => {
              const es = entry.trim();
              if (!es) return;

              const line = document.createElement("div");
              line.className = "pill-group";

              const esPill = document.createElement("span");
              esPill.className = "pill pill-es";
              const oper = /\boper:\s*(\S+)/.exec(es);
              const kind = oper ? stateKind(oper[1]) : "";
              if (kind && kind !== "up") esPill.classList.add(`pill-${kind}`);
              fillVirtualEsLabel(esPill, es, row["IP-VRF"]);

              line.append(esPill);
              lines.append(line);
            });
            details.append(
              makeCollapsibleDetailSection(`${sectionPrefix}:virtual-es`, "Virtual ES:", lines),
            );
          }

          // 5. VXLAN-interface
          const vxlanStr = row["VXLAN Interface"] || "-";
          if (vxlanStr !== "-") {
            const lines = document.createElement("div");
            lines.className = "pill-lines";
            vxlanStr.split(",").forEach((v) => {
              const line = document.createElement("div");
              line.className = "pill-group";

              const p = document.createElement("span");
              p.className = "pill pill-vxlan";
              p.textContent = v.trim();
              line.append(p);
              lines.append(line);
            });
            details.append(
              makeCollapsibleDetailSection(
                `${sectionPrefix}:vxlan-interface`,
                "VXLAN-interface:",
                lines,
              ),
            );
          }

          vrfDiv.append(details);
          nodeContent.append(vrfDiv);
        }
        nodeDiv.append(nodeContent);
        body.append(nodeDiv);
      }
      card.append(body);
      dom.servicesTreeView.append(card);
    }
  }

  function renderTreeControls() {
    const controls = document.createElement("div");
    controls.className = "tree-controls";

    const expandBtn = document.createElement("button");
    expandBtn.className = "btn btn-sm";
    expandBtn.type = "button";
    expandBtn.textContent = "📂 Expand All";
    expandBtn.addEventListener("click", () => {
      state.collapsedCards.clear();
      state.collapsedNodes.clear();
      state.collapsedSections.clear();
      dom.servicesTreeView.querySelectorAll(".bd-card.is-collapsed").forEach((card) => {
        card.classList.remove("is-collapsed");
        const body = card.querySelector(".bd-body");
        if (body) body.hidden = false;
        const header = card.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "true");
      });
      dom.servicesTreeView.querySelectorAll(".bd-node.is-collapsed").forEach((node) => {
        node.classList.remove("is-collapsed");
        const content = node.querySelector(".bd-node-content");
        if (content) content.hidden = false;
        const title = node.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "true");
      });
      dom.servicesTreeView.querySelectorAll(".bd-detail-section.is-collapsed").forEach((section) => {
        section.classList.remove("is-collapsed");
        const content = section.querySelector(".bd-detail-section-content");
        if (content) content.hidden = false;
        const header = section.querySelector(".bd-detail-section-header");
        if (header) header.setAttribute("aria-expanded", "true");
      });
    });

    const collapseBtn = document.createElement("button");
    collapseBtn.className = "btn btn-sm";
    collapseBtn.type = "button";
    collapseBtn.textContent = "📁 Collapse All";
    collapseBtn.addEventListener("click", () => {
      dom.servicesTreeView.querySelectorAll(".bd-card").forEach((card) => {
        const cardKey = card.dataset.cardKey;
        if (cardKey) state.collapsedCards.add(cardKey);
        card.classList.add("is-collapsed");
        const body = card.querySelector(".bd-body");
        if (body) body.hidden = true;
        const header = card.querySelector(".bd-header");
        if (header) header.setAttribute("aria-expanded", "false");
      });
      dom.servicesTreeView.querySelectorAll(".bd-node").forEach((node) => {
        const nodeKey = node.dataset.nodeKey;
        if (nodeKey) state.collapsedNodes.add(nodeKey);
        node.classList.add("is-collapsed");
        const content = node.querySelector(".bd-node-content");
        if (content) content.hidden = true;
        const title = node.querySelector(".bd-node-title");
        if (title) title.setAttribute("aria-expanded", "false");
      });
      dom.servicesTreeView.querySelectorAll(".bd-detail-section").forEach((section) => {
        const sectionKey = section.dataset.sectionKey;
        if (sectionKey) state.collapsedSections.add(sectionKey);
        section.classList.add("is-collapsed");
        const content = section.querySelector(".bd-detail-section-content");
        if (content) content.hidden = true;
        const header = section.querySelector(".bd-detail-section-header");
        if (header) header.setAttribute("aria-expanded", "false");
      });
    });

    controls.append(expandBtn, collapseBtn);
    return controls;
  }

  // A card view is rebuilt from scratch on every stream tick. Emptying the
  // scroll container clamps its offset to zero, and the fresh card bodies
  // start at the top, so without this each refresh would throw the reader
  // back to the top of the list and of every card they had scrolled into.
  // Only a refresh of the same page keeps its place: another report, or the
  // same lens asked something else, starts at the top as before.
  // The bodies scroll smoothly by stylesheet; the restore must not animate.
  function treePageKey() {
    return state.report ? `${state.report.name}?${queryParams()}` : "";
  }

  function treeScrollSnapshot() {
    const cards = new Map();
    const page = treePageKey();
    if (dom.servicesTreeView.dataset.page !== page) return { page, top: 0, cards };
    dom.servicesTreeView.querySelectorAll(".bd-card[data-card-key]").forEach((card) => {
      const body = card.querySelector(".bd-body");
      if (body && body.scrollTop) cards.set(card.dataset.cardKey, body.scrollTop);
    });
    return { page, top: dom.servicesTreeView.scrollTop, cards };
  }

  function restoreTreeScroll(snapshot) {
    dom.servicesTreeView.dataset.page = snapshot.page;
    snapshot.cards.forEach((top, key) => {
      const card = dom.servicesTreeView.querySelector(`.bd-card[data-card-key="${CSS.escape(key)}"]`);
      const body = card && card.querySelector(".bd-body");
      if (body) body.scrollTo({ top, behavior: "instant" });
    });
    dom.servicesTreeView.scrollTo({ top: snapshot.top, behavior: "instant" });
  }

  function renderBridgeDomainsTree(rows) {
    const scroll = treeScrollSnapshot();
    dom.servicesTreeView.replaceChildren();
    if (!rows || !rows.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No Services found.";
      dom.servicesTreeView.append(p);
      return;
    }

    dom.servicesTreeView.append(renderTreeControls());

    const bdRows = rows.filter((r) => r["Bridge Domain"] || r["MAC-VRF"] || r["Service Type"] === "Bridge Domain");
    const routerRows = rows.filter((r) => r["Router"] || r["IP-VRF"] || r["Service Type"] === "Router");

    if (bdRows.length > 0) {
      if (routerRows.length > 0) {
        const bdHeader = document.createElement("div");
        bdHeader.className = "services-section-header";
        bdHeader.textContent = "🌉 Bridge Domains";
        dom.servicesTreeView.append(bdHeader);
      }
      renderBridgeDomainsCards(bdRows);
    }

    if (routerRows.length > 0) {
      if (bdRows.length > 0) {
        const rtHeader = document.createElement("div");
        rtHeader.className = "services-section-header";
        rtHeader.textContent = "🔀 Routers";
        dom.servicesTreeView.append(rtHeader);
      }
      renderRoutersCards(routerRows);
    }

    restoreTreeScroll(scroll);
    executePendingJump();
  }

  /* ---------------------------------------------------------- lens tree */

  // The state a card, an entry or an item carries: 'up', 'warn', 'down' or
  // nothing, mapped onto the classes the services cards use.
  const LENS_CARD_STATE = { up: "up", warn: "degraded", down: "down" };
  const LENS_BADGE_CLASS = { up: "state-badge-up", warn: "state-badge-warn", down: "state-badge-down" };
  const LENS_PILL_CLASS = { up: "pill-up", warn: "pill-degraded", down: "pill-down" };

  function lensPill(text, kind) {
    const pill = document.createElement("span");
    pill.className = "pill";
    if (kind && LENS_PILL_CLASS[kind]) pill.classList.add(LENS_PILL_CLASS[kind]);
    pill.textContent = text;
    return pill;
  }

  function lensStateBadge(kind, text) {
    const badge = document.createElement("span");
    badge.className = `bd-state-badge ${LENS_BADGE_CLASS[kind] || ""}`.trim();
    badge.textContent = text;
    return badge;
  }

  // One detail line of an item: a label and a value, or pills for a list;
  // a pill of its own state where the value said so.
  function lensDetailRow(detail) {
    const row = document.createElement("div");
    row.className = "bd-detail-row";
    const label = document.createElement("strong");
    label.className = "bd-detail-label";
    label.textContent = `${detail.label}:`;
    row.append(label);
    if (Array.isArray(detail.value)) {
      const group = document.createElement("div");
      group.className = "pill-group";
      for (const item of detail.value) {
        const [text, kind] = Array.isArray(item) ? item : [item, detail.state];
        group.append(lensPill(String(text), kind));
      }
      row.append(group);
    } else {
      const value = document.createElement("span");
      value.className = "bd-detail-value";
      if (detail.state && LENS_PILL_CLASS[detail.state]) {
        value.append(lensPill(String(detail.value), detail.state));
      } else {
        value.textContent = String(detail.value);
      }
      row.append(value);
    }
    return row;
  }

  function lensItem(item) {
    const block = document.createElement("div");
    block.className = "bd-vrf";
    const header = document.createElement("div");
    header.className = "bd-vrf-header";
    const title = document.createElement("span");
    title.className = "bd-vrf-title";
    title.textContent = item.title;
    header.append(title);
    if (item.state) header.append(lensStateBadge(item.state, item.label || item.state.toUpperCase()));
    block.append(header);
    const details = document.createElement("div");
    details.className = "bd-details";
    for (const detail of item.details || []) details.append(lensDetailRow(detail));
    block.append(details);
    return block;
  }

  function lensEntry(cardKey, entry) {
    const nodeKey = `${cardKey}:node:${entry.title}`;
    const collapsed = state.collapsedNodes.has(nodeKey);
    const node = document.createElement("div");
    node.className = "bd-node";
    node.dataset.node = entry.title;
    node.dataset.nodeKey = nodeKey;
    if (collapsed) node.classList.add("is-collapsed");

    const title = document.createElement("div");
    title.className = "bd-node-title";
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    title.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const chevron = document.createElement("span");
    chevron.className = "bd-node-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "▼";
    const name = document.createElement("span");
    name.textContent = entry.title;
    title.append(chevron, name);
    if (entry.state) {
      const badge = lensStateBadge(entry.state, entry.label || entry.state.toUpperCase());
      badge.className = `bd-node-state ${LENS_BADGE_CLASS[entry.state] || ""}`.trim();
      title.append(badge);
    }
    if (entry.badge) {
      const count = document.createElement("span");
      count.className = "bd-node-count";
      count.textContent = entry.badge;
      title.append(count);
    }
    node.append(title);

    const content = document.createElement("div");
    content.className = "bd-node-content";
    if (collapsed) content.hidden = true;
    for (const item of entry.items || []) content.append(lensItem(item));
    node.append(content);

    const toggle = () => {
      const isCollapsed = node.classList.toggle("is-collapsed");
      content.hidden = isCollapsed;
      title.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
      if (isCollapsed) state.collapsedNodes.add(nodeKey);
      else state.collapsedNodes.delete(nodeKey);
    };
    title.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      toggle();
    });
    title.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
    return node;
  }

  function lensCard(card, index) {
    // An incident keeps its key as others come and go; its place does not.
    const cardKey = card.key
      ? `lens:${state.report.name}:key:${card.key}`
      : `lens:${state.report.name}:${index}:${card.title}`;
    if (state.report.name === "incidents" && card.key) {
      const seenKey = `${card.key}|${card.action === "unack" ? "acked" : "open"}`;
      if (!state.seenIncidentCards.has(seenKey)) {
        state.seenIncidentCards.add(seenKey);
        state.collapsedCards.add(cardKey);
      }
    }
    const collapsed = state.collapsedCards.has(cardKey);
    const el = document.createElement("div");
    el.className = `bd-card bd-state-${LENS_CARD_STATE[card.state] || "unknown"}`;
    el.dataset.cardKey = cardKey;
    if (collapsed) el.classList.add("is-collapsed");

    const header = document.createElement("div");
    header.className = "bd-header";
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");
    header.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const top = document.createElement("div");
    top.className = "bd-header-top";
    const chevron = document.createElement("span");
    chevron.className = "bd-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "▼";
    const icon = document.createElement("span");
    icon.className = "bd-icon";
    icon.textContent = card.icon || "🔎";
    const title = document.createElement("span");
    title.className = "bd-title";
    title.textContent = card.title;
    top.append(chevron, icon, title);
    // The label, where the lens gives one: an incident's colour is that of
    // down, but what it is is an error, not something reported down.
    if (card.state || card.label) top.append(lensStateBadge(card.state, card.label || card.state.toUpperCase()));
    if (card.badge) {
      const badge = document.createElement("span");
      badge.className = "bd-badge-count";
      badge.textContent = card.badge;
      top.append(badge);
    }
    if (card.action && card.key) top.append(ackButton(card));
    if (card.action === "unack") el.classList.add("is-acked");
    header.append(top);
    if (card.subtitle) {
      const sub = document.createElement("div");
      sub.className = "bd-header-sub";
      const text = document.createElement("span");
      text.className = "bd-rt-label";
      text.textContent = card.subtitle;
      sub.append(text);
      header.append(sub);
    }
    el.append(header);

    const body = document.createElement("div");
    body.className = "bd-body";
    if (collapsed) body.hidden = true;
    for (const entry of card.entries || []) body.append(lensEntry(cardKey, entry));
    el.append(body);

    const toggle = () => {
      const isCollapsed = el.classList.toggle("is-collapsed");
      body.hidden = isCollapsed;
      header.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
      if (isCollapsed) state.collapsedCards.add(cardKey);
      else state.collapsedCards.delete(cardKey);
    };
    header.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      toggle();
    });
    header.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
    return el;
  }

  /** The ACK all button, with how many incidents it would acknowledge. */
  function renderAckAll() {
    if (!dom.ackAllBtn) return;
    const open = (state.tree || []).filter((card) => card.action === "ack").length;
    dom.ackAllBtn.textContent = open ? `✓ ACK all (${open})` : "✓ ACK all";
    dom.ackAllBtn.disabled = !open;
  }

  async function ackAll() {
    const open = (state.tree || []).filter((card) => card.action === "ack").length;
    if (!open) return;
    const note = window.prompt(`Acknowledge all ${open} open incident(s) in this view?\n\nNote (optional):`, "");
    if (note === null) return; // cancelled
    dom.ackAllBtn.disabled = true;
    try {
      const res = await fetch("/api/ack-all", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note, inv_filter: dom.invFilter.value.trim() }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        window.alert(`ACK all failed: ${body.error || res.status}`);
      }
    } catch (_err) {
      window.alert("ACK all failed: the server did not answer");
    } finally {
      connect(); // the answer changes at once, not at the next refresh
    }
  }

  /**
   * ACK / un-ACK on an incident's card. Acknowledging takes the incident out
   * of the overview, the badges and the colours until a finding it did not
   * hold joins it, or it clears.
   */
  function ackButton(card) {
    const acking = card.action === "ack";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-ghost bd-ack";
    button.textContent = acking ? "✓ ACK" : "↺ Un-ACK";
    button.title = acking
      ? "Acknowledge: known, keep it out of the overview, badges and colours until it changes or clears"
      : "Take the acknowledgement off: count and colour it again";
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      let note = "";
      if (acking) {
        const typed = window.prompt(`Acknowledge "${card.title}"\n\nNote (optional):`, "");
        if (typed === null) return; // cancelled
        note = typed;
      }
      button.disabled = true;
      try {
        const res = await fetch(acking ? "/api/ack" : "/api/unack", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // The incident's id is of the fabric as this page shows it.
          body: JSON.stringify({ incident: card.key, note, inv_filter: dom.invFilter.value.trim() }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          // Not the error bar: the next table the stream pushes clears it,
          // and the click would look like it did nothing at all.
          window.alert(`${acking ? "ACK" : "Un-ACK"} failed: ${body.error || res.status}`);
        }
      } catch (_err) {
        window.alert(`${acking ? "ACK" : "Un-ACK"} failed: the server did not answer`);
      } finally {
        button.disabled = false;
        connect(); // the answer changes at once, not at the next refresh
      }
    });
    return button;
  }

  /* ------------------------------------------------------ watched prefixes */

  /** The watched prefixes: counted on the button, listed in its menu when open. */
  async function loadWatched({ open = false } = {}) {
    let watched = [];
    try {
      const res = await fetch("/api/watch");
      watched = (await res.json()).watched || [];
    } catch (_err) {
      return;
    }
    dom.watchBtn.textContent = watched.length ? `👁 Watched (${watched.length})` : "👁 Watched";
    if (open || !dom.watchMenu.hidden) renderWatchMenu(watched);
  }

  function renderWatchMenu(watched) {
    dom.watchMenu.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "menu-heading";
    heading.textContent = "Reported one by one, in every network-instance";
    dom.watchMenu.append(heading);
    const note = document.createElement("div");
    note.className = "muted watch-note";
    note.textContent = "Default routes and every node's system address are always watched.";
    dom.watchMenu.append(note);
    for (const prefix of watched) {
      const row = document.createElement("div");
      row.className = "watch-row";
      const text = document.createElement("span");
      text.textContent = prefix;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-ghost";
      remove.textContent = "✕";
      remove.title = `Stop watching ${prefix}`;
      remove.addEventListener("click", () => changeWatch("/api/unwatch", prefix));
      row.append(text, remove);
      dom.watchMenu.append(row);
    }
    const form = document.createElement("form");
    form.className = "watch-add";
    const input = document.createElement("input");
    input.className = "input";
    input.placeholder = "10.1.4.16 or 6.6.6.0/24";
    input.spellcheck = false;
    const add = document.createElement("button");
    add.type = "submit";
    add.className = "btn";
    add.textContent = "Watch";
    form.append(input, add);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (value) changeWatch("/api/watch", value);
    });
    dom.watchMenu.append(form);
    input.focus();
  }

  async function changeWatch(url, prefix) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefix }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        window.alert(body.error || `watching failed (${res.status})`);
      }
    } catch (_err) {
      window.alert("watching failed: the server did not answer");
    }
    loadWatched({ open: true });
  }

  // What a lens found, as cards: one per thing found, the nodes inside it,
  // and under each node what that node reports - the same fold the
  // services pages give a fabric.
  function renderLensTree(cards) {
    const scroll = treeScrollSnapshot();
    dom.servicesTreeView.replaceChildren();
    if (!cards.length) {
      const p = document.createElement("p");
      p.className = "empty";
      const missing = missingParams();
      p.textContent = missing.length
        ? `Enter ${missing.map((spec) => spec.label.toLowerCase()).join(" and ")} above to ask.`
        : "Nothing found.";
      dom.servicesTreeView.append(p);
      return;
    }
    dom.servicesTreeView.append(renderTreeControls());
    cards.forEach((card, index) => dom.servicesTreeView.append(lensCard(card, index)));
    restoreTreeScroll(scroll);
  }

  /* ---------------------------------------------------------- path graph */

  const PATH_BOX = { w: 168, h: 46, colGap: 96, rowGap: 22, top: 30, left: 12 };

  function pathSvg(name, attrs, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function pathTrim(text, max) {
    const value = String(text || "");
    return value.length > max ? value.slice(0, max - 1) + "…" : value;
  }

  // A node name is told apart by its tail - containerlab prefixes every node
  // of a lab the same way - so a title keeps its end rather than its start.
  function pathTrimHead(text, max) {
    const value = String(text || "");
    return value.length > max ? "…" + value.slice(value.length - max + 1) : value;
  }

  // The walk as a picture: one column per hop, a box per lookup, an edge to
  // each lookup it leads to - so ECMP fans out across a column and converges
  // where branches meet, and a branch that dies goes to a red stop.
  function renderPathGraph(graph) {
    dom.pathGraphView.replaceChildren();
    if (!graph || !graph.nodes.length) {
      const p = document.createElement("p");
      p.className = "empty";
      const missing = missingParams();
      p.textContent = missing.length
        ? `Enter ${missing.map((spec) => spec.label.toLowerCase()).join(" and ")} above to walk the path.`
        : "Nothing to draw.";
      dom.pathGraphView.append(p);
      return;
    }

    // Layout: columns by hop, rows in the order the server listed them.
    const hops = [...new Set(graph.nodes.map((n) => n.hop))].sort((a, b) => a - b);
    const column = new Map(hops.map((hop, index) => [hop, index]));
    const rowsOf = new Map();
    const place = new Map();
    for (const node of graph.nodes) {
      const row = rowsOf.get(node.hop) || 0;
      rowsOf.set(node.hop, row + 1);
      place.set(node.id, {
        x: PATH_BOX.left + column.get(node.hop) * (PATH_BOX.w + PATH_BOX.colGap),
        y: PATH_BOX.top + row * (PATH_BOX.h + PATH_BOX.rowGap),
      });
    }
    const width = PATH_BOX.left * 2 + hops.length * (PATH_BOX.w + PATH_BOX.colGap) - PATH_BOX.colGap;
    const height = PATH_BOX.top + Math.max(...rowsOf.values()) * (PATH_BOX.h + PATH_BOX.rowGap);

    const summary = document.createElement("div");
    summary.className = "path-graph-summary";
    const reached = graph.nodes.filter((n) => n.outcomes.includes("neighbor") || n.outcomes.includes("local-ip")).length;
    const stopped = graph.nodes.filter((n) => n.state === "down").length;
    const parts = [
      `to <strong>${graph.destination}</strong>`,
      `<strong>${hops.length}</strong> hop${hops.length === 1 ? "" : "s"}`,
      `<strong>${graph.nodes.length}</strong> lookups`,
      `<strong>${reached}</strong> reached`,
    ];
    if (stopped) parts.push(`<strong>${stopped}</strong> stopped`);
    summary.innerHTML = parts.join(" · ");
    dom.pathGraphView.append(summary);

    const svg = pathSvg("svg", {
      class: "path-graph",
      width,
      height,
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `Path to ${graph.destination}`,
    });
    const defs = pathSvg("defs", {});
    for (const kind of ["", "up", "down"]) {
      const marker = pathSvg("marker", {
        id: `path-arrow-${kind || "plain"}`,
        viewBox: "0 0 10 10",
        refX: 9,
        refY: 5,
        markerWidth: 7,
        markerHeight: 7,
        orient: "auto-start-reverse",
      });
      marker.append(pathSvg("path", { d: "M0,0 L10,5 L0,10 z", class: `path-arrow ${kind ? "path-arrow-" + kind : ""}`.trim() }));
      defs.append(marker);
    }
    svg.append(defs);

    for (const hop of hops) {
      const x = PATH_BOX.left + column.get(hop) * (PATH_BOX.w + PATH_BOX.colGap) + PATH_BOX.w / 2;
      svg.append(pathSvg("text", { x, y: 16, "text-anchor": "middle", class: "path-hop-label" }, `hop ${hop}`));
    }

    // Labels of the edges leaving one box are spread along their curves,
    // or the ones fanning out of a leaf would print over each other.
    const leaving = new Map();
    for (const edge of graph.edges) leaving.set(edge.from, (leaving.get(edge.from) || 0) + 1);
    const labelled = new Map();
    for (const edge of graph.edges) {
      const from = place.get(edge.from);
      const to = place.get(edge.to);
      if (!from || !to) continue;
      const siblings = leaving.get(edge.from) || 1;
      const index = labelled.get(edge.from) || 0;
      labelled.set(edge.from, index + 1);
      const x1 = from.x + PATH_BOX.w;
      const y1 = from.y + PATH_BOX.h / 2;
      const x2 = to.x;
      const y2 = to.y + PATH_BOX.h / 2;
      const bend = Math.max(30, (x2 - x1) / 2);
      const d = `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
      const kind = edge.state === "up" || edge.state === "down" ? edge.state : "";
      svg.append(
        pathSvg("path", {
          d,
          class: `path-edge ${kind ? "path-edge-" + kind : ""}`.trim(),
          "marker-end": `url(#path-arrow-${kind || "plain"})`,
        })
      );
      if (edge.label) {
        // Near the source, where edges leaving one box are still apart, and
        // further along for each next sibling.
        const t = siblings > 1 ? 0.2 + (0.35 * index) / (siblings - 1) : 0.28;
        const lx = x1 + (x2 - x1) * t;
        const ly = y1 + (y2 - y1) * t - 5;
        const text = pathTrim(edge.label, 24);
        const bg = pathSvg("rect", {
          x: lx - text.length * 3.1 - 3,
          y: ly - 9,
          width: text.length * 6.2 + 6,
          height: 12,
          rx: 3,
          class: "path-edge-label-bg",
        });
        svg.append(bg, pathSvg("text", { x: lx, y: ly, "text-anchor": "middle", class: "path-edge-label" }, text));
      }
    }

    for (const node of graph.nodes) {
      const at = place.get(node.id);
      const group = pathSvg("g", { transform: `translate(${at.x},${at.y})` });
      const title = pathSvg("title", {}, [node.title, node.subtitle, ...(node.details || [])].filter(Boolean).join("\n"));
      group.append(
        title,
        pathSvg("rect", {
          width: PATH_BOX.w,
          height: PATH_BOX.h,
          class: `path-box ${node.state ? "path-box-" + node.state : ""}`.trim(),
        }),
        pathSvg("text", { x: 10, y: node.subtitle ? 19 : 28, class: "path-box-title" }, pathTrimHead(node.title, 22)),
      );
      if (node.subtitle) {
        group.append(pathSvg("text", { x: 10, y: 35, class: "path-box-sub" }, pathTrim(node.subtitle, 27)));
      }
      svg.append(group);
    }
    dom.pathGraphView.append(svg);
  }

  function renderBody() {
    // Overview and Topology own the main area; a table would be drawn over them.
    if (state.report && isPanelReport(state.report.name)) return;
    const rows = filteredRows();

    // The services tree draws a fabric, not a verdict on two of them, so a
    // comparison is always shown as a table.
    if (
      !state.diff &&
      state.report &&
      ["bridge_domains", "services", "routers"].includes(state.report.name) &&
      state.viewMode === "tree"
    ) {
      dom.tableWrap.hidden = true;
      dom.pathGraphView.hidden = true;
      dom.servicesTreeView.hidden = false;
      renderBridgeDomainsTree(rows);
      dom.rowCount.textContent = `${rows.length} service entry/entries`;
      return;
    }

    if (!state.diff && hasGraphView(state.report) && state.viewMode === "graph") {
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = true;
      dom.pathGraphView.hidden = false;
      renderPathGraph(state.graph);
      if (state.rows.length) {
        dom.rowCount.textContent = `${state.graph ? state.graph.nodes.length : 0} lookup(s), ${state.rows.length} row(s)`;
      }
      return;
    }
    dom.pathGraphView.hidden = true;

    if (!state.diff && isLens(state.report) && state.viewMode === "tree") {
      dom.tableWrap.hidden = true;
      dom.servicesTreeView.hidden = false;
      renderLensTree(state.tree || []);
      if (state.rows.length) {
        dom.rowCount.textContent = `${state.tree ? state.tree.length : 0} card(s), ${state.rows.length} row(s)`;
      }
      return;
    }

    dom.servicesTreeView.hidden = true;
    dom.pathGraphView.hidden = true;
    dom.tableWrap.hidden = false;

    if (state.sort.column) {
      const { column, dir } = state.sort;
      rows.sort((a, b) => dir * compare(a[column], b[column]));
    }
    const identity = identityColumn(state.rows);
    const columns = visibleColumns();
    const shown = rows.slice(0, state.windowSize);
    const fragment = document.createDocumentFragment();
    // Not when comparing two runs: there the colour is the comparison's verdict.
    const toned = !state.diff && state.report ? ROW_TONES[state.report.name] : null;

    for (const row of shown) {
      const key = rowKey(row, identity);
      const previous = state.previous.get(key);
      const tr = document.createElement("tr");
      if (state.diff) {
        // The verdict of the comparison, not the flash of a live update.
        tr.className = "diff-" + String(row[DIFF_STATUS] ?? "same");
      } else if (!state.firstPaint && previous === undefined) {
        tr.className = "added";
      }
      const tone =
        toned && !(toned.quiet && toned.quiet(row))
          ? toned.tone(String(row[toned.column] ?? "").toLowerCase())
          : "";
      if (tone) tr.classList.add("tone-" + tone);
      for (const column of columns) {
        const value = row[column] ?? "";
        const td = document.createElement("td");
        const linked =
          !state.diff && state.report && state.report.name === "bgp_peers"
            ? peerRoutesCell(row, column, value)
            : null;
        if (linked) td.append(linked);
        else td.textContent = value;
        if (tone && column === toned.column) td.classList.add("tone-cell");
        const changes = state.diff ? row["_changes"] : null;
        if (changes && changes[column]) td.classList.add("diff-cell");
        let stateClass = STATE_CLASSES[String(value).toLowerCase()];
        if (state.report && state.report.name === "bgp_peers" && column.toLowerCase().includes("state")) {
          // The table shows an established session as 'up'; anything else
          // is a session still trying.
          const session = String(value).toLowerCase();
          stateClass = session === "up" || session === "established" ? "state-established" : "state-down";
        }
        if (stateClass) td.classList.add(stateClass);
        else if (isNumeric(value) && value !== "") td.classList.add("num");
        if (
          !state.diff &&
          !state.firstPaint &&
          previous !== undefined &&
          previous[column] !== undefined &&
          previous[column] !== value
        ) {
          td.classList.add("changed");
        }
        tr.append(td);
      }
      tr.append(fillerCell("td"));
      fragment.append(tr);
    }
    // Remember every row of the payload, not just the ones on screen, so that
    // changing a filter or scrolling does not make old rows look new.
    const next = new Map();
    for (const row of state.rows) next.set(rowKey(row, identity), row);

    dom.body.replaceChildren(fragment);
    state.previous = next;
    state.firstPaint = false;
    dom.empty.hidden = rows.length > 0;
    const suffix = rows.length === state.rows.length ? "" : ` of ${state.rows.length}`;
    const windowed = shown.length < rows.length ? ` (showing ${shown.length})` : "";
    const plural = rows.length === 1 ? "" : "s";
    dom.rowCount.textContent = `${rows.length} row${plural}${suffix}${windowed}`;
  }

  async function exportPayload() {
    return { columns: visibleColumns(), rows: filteredRows() };
  }

  // What -o json gives on the CLI: the records, not the table's cells.
  function exportRecords() {
    return isLens(state.report) && Array.isArray(state.records) ? state.records : null;
  }

  function downloadText(filename, mime, text) {
    const blob = new Blob([text], { type: mime });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function exportMenuButton(text, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "menu-item";
    button.textContent = text;
    button.addEventListener("click", () => {
      dom.exportMenu.hidden = true;
      onClick();
    });
    return button;
  }

  function renderExportMenu() {
    dom.exportMenu.replaceChildren(
      exportMenuButton("CSV", () => exportReport("csv")),
      exportMenuButton("JSON", () => exportReport("json")),
      exportMenuButton("YAML", () => exportReport("yaml"))
    );
  }

  async function exportReport(format) {
    if (!state.report) return;
    try {
      const { columns, rows } = await exportPayload();
      const base = state.report.name;
      if (format === "csv") {
        const escape = (value) => {
          const text = String(value ?? "");
          return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
        };
        const lines = [columns.join(",")];
        for (const row of rows) lines.push(columns.map((c) => escape(row[c])).join(","));
        downloadText(`${base}.csv`, "text/csv", lines.join("\n"));
      } else if (format === "json") {
        const payload = exportRecords() || rows;
        downloadText(`${base}.json`, "application/json", JSON.stringify(payload, null, 2));
      } else {
        const yaml = rows
          .map((row) => {
            const lines = ["-"];
            for (const column of columns) {
              const value = row[column];
              if (value === undefined || value === "") continue;
              lines.push(`  ${column}: ${JSON.stringify(String(value))}`);
            }
            return lines.join("\n");
          })
          .join("\n");
        downloadText(`${base}.yaml`, "text/yaml", yaml);
      }
    } catch (err) {
      showErrors([{ node: "export", error: String(err.message || err) }]);
    }
  }

  /* ---------------------------------------------------------- inventory */

  function transferIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", "node-xfer");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      "M2 5h9.5m0 0L9 2.75M11.5 5 9 7.25M14 11H4.5m0 0L7 8.75M4.5 11 7 13.25"
    );
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }

  let inventoryKey = "";
  const getCounts = new Map();

  // A Get is over in milliseconds, so sampling "one is in flight" would almost
  // never catch it. The node's Get counter moving between two polls is what
  // shows gNMI activity.
  function pollsGnmi(host) {
    const seen = getCounts.get(host.name);
    getCounts.set(host.name, host.gets);
    return host.getting || (seen !== undefined && host.gets > seen);
  }

  async function loadInventory() {
    try {
      const res = await fetch("/api/inventory");
      const data = await res.json();
      const hosts = data.hosts.map((host) => ({
        ...host,
        active: pollsGnmi(host),
      }));
      const key = hosts
        .map(
          (h) => `${h.name}:${h.connected}:${h.streaming}:${h.active}:${h.error || ""}`
        )
        .join("|");
      if (key === inventoryKey) return;
      inventoryKey = key;
      dom.nodeList.replaceChildren(
        ...hosts.map((host) => {
          const item = document.createElement("li");
          const dot = document.createElement("span");
          dot.className =
            "dot " +
            (!host.connected ? "error" : host.streaming ? "live" : "paused");
          const name = document.createElement("span");
          name.className = "node-name";
          name.textContent = host.name;
          let status;
          if (host.getting) status = "gNMI Get in flight";
          else if (host.error) status = host.error;
          else if (host.active) status = "gNMI Get just completed";
          else if (host.streaming) status = "streaming";
          else status = "connected, not subscribed";
          item.title = `${host.name} (${host.hostname}) - ${status}`;
          item.append(dot, name);
          if (host.active) item.append(transferIcon());
          return item;
        })
      );
      const up = hosts.filter((h) => h.connected).length;
      dom.nodeSummary.textContent = `${up}/${hosts.length} up`;
    } catch (_err) {
      inventoryKey = "";
      dom.nodeSummary.textContent = "unavailable";
    }
  }

  /* ------------------------------------------------------- sidebar split */

  const SIDE_SPLIT_MIN = 72;
  const SIDE_SPLIT_DEFAULT = 0.38;
  let sideSplitFrac = SIDE_SPLIT_DEFAULT;

  function loadSideSplitFrac() {
    try {
      const stored = parseFloat(localStorage.getItem("fcli-side-split"));
      if (stored > 0 && stored < 1) return stored;
    } catch (_err) {
      /* storage may be unavailable */
    }
    return SIDE_SPLIT_DEFAULT;
  }

  function saveSideSplitFrac(frac) {
    sideSplitFrac = frac;
    try {
      localStorage.setItem("fcli-side-split", String(frac));
    } catch (_err) {
      /* storage may be unavailable */
    }
  }

  /** Size the nodes pane to *frac* of the split, leaving reports the rest. */
  function applySideSplit(frac, persist) {
    const split = dom.sideSplit;
    const nodes = dom.nodesBlock;
    const splitter = dom.sideSplitter;
    if (!split || !nodes || !splitter) return;
    const avail = split.clientHeight - splitter.offsetHeight;
    if (avail <= 0) return;
    const min = Math.min(SIDE_SPLIT_MIN, Math.floor(avail / 3));
    const height = Math.round(Math.min(avail - min, Math.max(min, avail * frac)));
    nodes.style.flexBasis = `${height}px`;
    const used = height / avail;
    sideSplitFrac = used;
    splitter.setAttribute("aria-valuenow", String(Math.round(used * 100)));
    if (persist) saveSideSplitFrac(used);
  }

  function initSideSplit() {
    const splitter = dom.sideSplitter;
    const split = dom.sideSplit;
    if (!splitter || !split) return;
    sideSplitFrac = loadSideSplitFrac();
    applySideSplit(sideSplitFrac, false);

    splitter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      const sidebar = split.closest(".sidebar");
      if (sidebar) sidebar.classList.add("is-resizing");
      const box = split.getBoundingClientRect();
      const onMove = (move) => {
        const avail = box.height - splitter.offsetHeight;
        if (avail <= 0) return;
        applySideSplit((box.bottom - move.clientY - splitter.offsetHeight / 2) / avail, false);
      };
      const onUp = () => {
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        if (sidebar) sidebar.classList.remove("is-resizing");
        applySideSplit(sideSplitFrac, true);
      };
      splitter.addEventListener("pointermove", onMove);
      splitter.addEventListener("pointerup", onUp);
      splitter.addEventListener("pointercancel", onUp);
    });

    splitter.addEventListener("keydown", (event) => {
      let next = sideSplitFrac;
      if (event.key === "ArrowUp") next -= 0.05;
      else if (event.key === "ArrowDown") next += 0.05;
      else if (event.key === "Home") next = 0.15;
      else if (event.key === "End") next = 0.85;
      else return;
      event.preventDefault();
      applySideSplit(next, true);
    });

    if (window.ResizeObserver) {
      new ResizeObserver(() => applySideSplit(sideSplitFrac, false)).observe(split);
    }
  }

  /* ------------------------------------------------------------- wiring */

  if (dom.navBack) {
    dom.navBack.addEventListener("click", () => {
      if (state.navIndex > 0) history.back();
    });
  }
  if (dom.navForward) {
    dom.navForward.addEventListener("click", () => {
      if (state.navIndex < state.navStack.length - 1) history.forward();
    });
  }
  window.addEventListener("popstate", (event) => {
    const snap = event.state && event.state.page;
    if (!snap || !state.reports.length) return;
    const idx = state.navStack.findIndex((entry) => entry.id === snap.id);
    if (idx >= 0) {
      state.navIndex = idx;
    } else {
      state.navStack = [snap];
      state.navIndex = 0;
      if (snap.id > navSeq) navSeq = snap.id;
    }
    restoreNavSnap(snap);
  });

  dom.reportSearch.addEventListener("input", debounce(renderReportList, 120));
  dom.globalSearch.addEventListener(
    "input",
    debounce(() => {
      state.windowSize = WINDOW_STEP;
      saveReportPreferences();
      updateFilterUI();
      renderBody();
    }, 150)
  );
  dom.invFilter.addEventListener(
    "change",
    () => {
      saveReportPreferences();
      updateFilterUI();
      if (state.report && state.report.name === "topology") {
        state.topoKey = "";
        loadTopology();
      } else {
        connect();
      }
    }
  );
  dom.refresh.addEventListener(
    "change",
    () => {
      saveReportPreferences();
      connect();
    }
  );

  dom.clearFiltersBtn.addEventListener("click", clearAllFilters);

  const openReport = (name) => {
    const report = state.reports.find((r) => r.name === name);
    if (report) selectReport(report);
  };

  if (dom.kpiCardHealth) dom.kpiCardHealth.addEventListener("click", () => openReport("incidents"));

  if (dom.baselineBtn) {
    dom.baselineBtn.addEventListener("click", async () => {
      dom.baselineBtn.disabled = true;
      try {
        const res = await fetch("/api/baseline", { method: "POST" });
        const status = await res.json();
        const at = status.baseline_at ? new Date(status.baseline_at * 1000).toLocaleTimeString() : "now";
        dom.streamInfo.textContent = `baseline set at ${at}`;
        // Asking for the drift right after setting the baseline shows it
        // empty, which is the point: everything from here on is a change.
        state.reportParams.set("since", "baseline");
        renderReportParams();
        updateFilterUI();
        connect();
        syncCurrentVisit();
      } catch (_err) {
        showErrors([{ node: "server", error: "setting the baseline failed" }]);
      } finally {
        dom.baselineBtn.disabled = false;
      }
    });
  }

  if (dom.topoOverlay) {
    dom.topoOverlay.addEventListener("change", () => {
      state.topoOverlay = dom.topoOverlay.value;
      try {
        localStorage.setItem("fcli-topo-overlay", state.topoOverlay);
      } catch (_err) {
        /* storage may be unavailable */
      }
      if (state.topology) renderTopology(state.topology);
    });
  }

  if (dom.chatTriage) dom.chatTriage.addEventListener("click", triage);
  if (dom.ackAllBtn) dom.ackAllBtn.addEventListener("click", ackAll);

  if (dom.kpiCardBd) {
    dom.kpiCardBd.addEventListener("click", () => {
      const report = state.reports.find((r) => r.name === "bridge_domains");
      if (report) selectReport(report);
    });
  }

  if (dom.kpiCardRouters) {
    dom.kpiCardRouters.addEventListener("click", () => {
      const report = state.reports.find((r) => r.name === "routers");
      if (report) selectReport(report);
    });
  }

  dom.viewModeBtn.addEventListener("click", () => {
    state.viewMode = nextViewMode();
    if (state.report) {
      try {
        localStorage.setItem(`fcli-viewmode-${state.report.name}`, state.viewMode);
      } catch (_err) {}
    }
    dom.viewModeBtn.textContent = viewModeLabel();
    renderBody();
  });

  dom.pause.addEventListener("click", () => {
    state.paused = !state.paused;
    dom.pause.textContent = state.paused ? "▶ Resume" : "⏸ Pause";
    if (state.paused) {
      if (state.source) state.source.close();
      state.source = null;
      setLive("paused", "paused");
    } else if (state.report && state.report.name === "topology") {
      loadTopology();
    } else {
      connect();
    }
  });

  dom.topoPortLabels.addEventListener("change", () => {
    try {
      localStorage.setItem("fcli-topo-ports", dom.topoPortLabels.checked ? "1" : "");
    } catch (_err) {
      /* storage may be unavailable */
    }
    if (state.topology) renderTopology(state.topology);
  });

  if (dom.topoMaxBw) {
    dom.topoMaxBw.addEventListener("change", onTopoMaxBwChange);
    dom.topoMaxBw.addEventListener("input", onTopoMaxBwChange);
  }
  if (dom.topoMaxBwUnit) {
    dom.topoMaxBwUnit.addEventListener("change", onTopoMaxBwChange);
  }

  dom.topoZoomIn.addEventListener("click", () =>
    setTopoZoom(topoZoom() * TOPO_ZOOM_STEP, topoCanvasCenter())
  );
  dom.topoZoomOut.addEventListener("click", () =>
    setTopoZoom(topoZoom() / TOPO_ZOOM_STEP, topoCanvasCenter())
  );
  dom.topoZoomLevel.addEventListener("click", () => setTopoZoom(1, topoCanvasCenter()));
  dom.topoZoomFit.addEventListener("click", fitTopoZoom);

  dom.topoCanvas.addEventListener(
    "wheel",
    (event) => {
      // A bare wheel scrolls the canvas; ctrl (or a trackpad pinch, which
      // arrives as one) zooms, as it does in a map.
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setTopoZoom(topoZoom() * Math.exp(-event.deltaY * 0.002), {
        x: event.clientX,
        y: event.clientY,
      });
    },
    { passive: false }
  );

  // Dragging the canvas pans it. A drag that moves is not a click, so the
  // selection a node or a link would otherwise take is swallowed below.
  let topoPan = null;
  let topoPanned = false;
  dom.topoCanvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 && event.button !== 1) return;
    topoPanned = false;
    topoPan = {
      x: event.clientX,
      y: event.clientY,
      left: dom.topoCanvas.scrollLeft,
      top: dom.topoCanvas.scrollTop,
      moved: false,
    };
  });
  window.addEventListener("pointermove", (event) => {
    if (!topoPan) return;
    const dx = event.clientX - topoPan.x;
    const dy = event.clientY - topoPan.y;
    if (!topoPan.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
    topoPan.moved = true;
    dom.topoCanvas.classList.add("is-panning");
    dom.topoCanvas.scrollLeft = topoPan.left - dx;
    dom.topoCanvas.scrollTop = topoPan.top - dy;
  });
  window.addEventListener("pointerup", () => {
    if (!topoPan) return;
    topoPanned = topoPan.moved;
    topoPan = null;
    dom.topoCanvas.classList.remove("is-panning");
  });
  dom.topoCanvas.addEventListener(
    "click",
    (event) => {
      if (!topoPanned) return;
      event.stopPropagation();
    },
    true
  );

  document.addEventListener("keydown", (event) => {
    if (dom.topologyView.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target && target.closest && target.closest("input, textarea, select")) return;
    if (event.key === "+" || event.key === "=") setTopoZoom(topoZoom() * TOPO_ZOOM_STEP, topoCanvasCenter());
    else if (event.key === "-" || event.key === "_") setTopoZoom(topoZoom() / TOPO_ZOOM_STEP, topoCanvasCenter());
    else if (event.key === "0") setTopoZoom(1, topoCanvasCenter());
    else if (event.key === "f" || event.key === "F") fitTopoZoom();
    else return;
    event.preventDefault();
  });

  if (window.ResizeObserver) {
    // A fitted drawing follows the canvas, which changes with the window but
    // also when the detail panel opens beside it.
    let fitted = 0;
    const observer = new ResizeObserver(() => {
      if (dom.topologyView.hidden || !state.topoFit) return;
      const zoom = topoFitZoom();
      // Resizing the drawing resizes the canvas back when a scrollbar comes or
      // goes, so ignore what a redraw would not move.
      if (Math.abs(zoom - fitted) < 0.005) return;
      fitted = zoom;
      applyTopoZoom();
    });
    observer.observe(dom.topoCanvas);
  }

  dom.columnsBtn.addEventListener("click", () => {
    dom.columnsMenu.hidden = !dom.columnsMenu.hidden;
  });
  document.addEventListener("click", (event) => {
    if (!dom.columnsMenu.hidden && !event.target.closest(".menu")) {
      dom.columnsMenu.hidden = true;
    }
    if (!dom.compareMenu.hidden && !event.target.closest(".menu")) {
      dom.compareMenu.hidden = true;
    }
    if (!dom.exportMenu.hidden && !event.target.closest(".menu")) {
      dom.exportMenu.hidden = true;
    }
    if (dom.watchMenu && !dom.watchMenu.hidden && !event.target.closest(".menu")) {
      dom.watchMenu.hidden = true;
    }
  });

  if (dom.watchBtn) {
    dom.watchBtn.addEventListener("click", () => {
      const opening = dom.watchMenu.hidden;
      dom.watchMenu.hidden = !opening;
      if (opening) loadWatched({ open: true });
    });
  }

  dom.compareBtn.addEventListener("click", () => {
    const opening = dom.compareMenu.hidden;
    dom.compareMenu.hidden = !opening;
    if (opening) renderCompareMenu();
  });
  dom.exportBtn.addEventListener("click", () => {
    const opening = dom.exportMenu.hidden;
    dom.exportMenu.hidden = !opening;
    if (opening) renderExportMenu();
  });
  dom.diffExit.addEventListener("click", () => exitDiff());
  dom.diffSame.addEventListener("change", () => {
    // Same comparison, asked again for the rows it left out.
    if (state.diff) showDiff({ against: state.diff.against, nodes: state.diff.nodes });
  });

  dom.topoExportDrawio.addEventListener("click", exportTopologyDrawio);

  dom.tableWrap.addEventListener("scroll", () => {
    const { scrollTop, scrollHeight, clientHeight } = dom.tableWrap;
    if (scrollHeight - scrollTop - clientHeight < 400) {
      const rows = filteredRows();
      if (state.windowSize < rows.length) {
        state.windowSize += WINDOW_STEP;
        renderBody();
      }
    }
  });

  dom.themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("fcli-theme", next);
    } catch (_err) {
      /* storage may be unavailable */
    }
  });

  try {
    const stored = localStorage.getItem("fcli-theme");
    if (stored) document.documentElement.dataset.theme = stored;
    dom.topoPortLabels.checked = Boolean(localStorage.getItem("fcli-topo-ports"));
  } catch (_err) {
    /* storage may be unavailable */
  }
  restoreTopoZoom();
  restoreTopoFabric();
  try {
    state.topoOverlay = localStorage.getItem("fcli-topo-overlay") || "traffic";
  } catch (_err) {
    /* storage may be unavailable */
  }
  restoreTopoMaxBw();
  initSideSplit();

  /* ---------------------------------------------------------- markdown */

  /* A small CommonMark subset for what the models actually emit: headings,
     lists, fenced code, pipe tables, blockquotes and inline emphasis. Nodes are
     built with the DOM rather than innerHTML, so model output can never inject
     markup. */

  // Underscore emphasis needs a word boundary, or snake_case names the agent
  // deals in all day (bgp_rib_evpn_2, mac_table) would come out italicised.
  const MD_INLINE =
    /(`+)([\s\S]+?)\1|\*\*([\s\S]+?)\*\*|(?<!\w)__([\s\S]+?)__(?!\w)|~~([\s\S]+?)~~|\*(\S[^*\n]*?)\*|(?<!\w)_(\S[^_\n]*?)_(?!\w)|\[([^\]\n]+)\]\(([^)\s]+)\)/;

  /** Append *text* to *parent*, turning inline markdown into elements. */
  function mdInline(text, parent) {
    let rest = String(text);
    while (rest) {
      const match = MD_INLINE.exec(rest);
      if (!match) break;
      if (match.index > 0) parent.append(rest.slice(0, match.index));
      const [full, , codeText, strong1, strong2, strike, em1, em2, linkText, href] =
        match;
      if (codeText !== undefined) {
        const code = document.createElement("code");
        code.textContent = codeText.trim();
        parent.append(code);
      } else if (strong1 !== undefined || strong2 !== undefined) {
        const strong = document.createElement("strong");
        mdInline(strong1 !== undefined ? strong1 : strong2, strong);
        parent.append(strong);
      } else if (strike !== undefined) {
        const del = document.createElement("del");
        mdInline(strike, del);
        parent.append(del);
      } else if (em1 !== undefined || em2 !== undefined) {
        const em = document.createElement("em");
        mdInline(em1 !== undefined ? em1 : em2, em);
        parent.append(em);
      } else if (linkText !== undefined) {
        // Anything but a plain web link stays inert text.
        if (/^(https?:|mailto:)/i.test(href)) {
          const link = document.createElement("a");
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          mdInline(linkText, link);
          parent.append(link);
        } else {
          parent.append(full);
        }
      }
      rest = rest.slice(match.index + full.length);
    }
    if (rest) parent.append(rest);
  }

  function mdParagraph(lines) {
    const p = document.createElement("p");
    lines.forEach((line, index) => {
      if (index) p.append(document.createElement("br"));
      mdInline(line, p);
    });
    return p;
  }

  function mdIndent(line) {
    const match = /^\s*/.exec(line);
    return match[0].replace(/\t/g, "  ").length;
  }

  function mdListItem(line) {
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (bullet) return { ordered: false, text: bullet[1] };
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ordered) return { ordered: true, text: ordered[1] };
    return null;
  }

  function mdTableRow(line) {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((cell) => cell.trim());
  }

  /** Parse *text* into an array of block-level DOM nodes. */
  function mdBlocks(text) {
    const lines = String(text).replace(/\r\n/g, "\n").split("\n");
    const nodes = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i += 1;
        continue;
      }
      const fence = /^\s*(```|~~~)(.*)$/.exec(line);
      if (fence) {
        const body = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        const lang = fence[2].trim();
        if (lang) code.className = "lang-" + lang.split(/\s+/)[0];
        code.textContent = body.join("\n");
        pre.append(code);
        nodes.push(pre);
        continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const el = document.createElement("h" + Math.min(heading[1].length, 4));
        mdInline(heading[2].replace(/\s+#+\s*$/, ""), el);
        nodes.push(el);
        i += 1;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        nodes.push(document.createElement("hr"));
        i += 1;
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        const quote = document.createElement("blockquote");
        mdBlocks(quoted.join("\n")).forEach((node) => quote.append(node));
        nodes.push(quote);
        continue;
      }
      if (
        line.includes("|") &&
        i + 1 < lines.length &&
        /^\s*\|?[\s:-]*-[\s|:-]*$/.test(lines[i + 1]) &&
        lines[i + 1].includes("-")
      ) {
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        for (const cell of mdTableRow(line)) {
          const th = document.createElement("th");
          mdInline(cell, th);
          headRow.append(th);
        }
        thead.append(headRow);
        table.append(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const row = document.createElement("tr");
          for (const cell of mdTableRow(lines[i])) {
            const td = document.createElement("td");
            mdInline(cell, td);
            row.append(td);
          }
          tbody.append(row);
          i += 1;
        }
        table.append(tbody);
        nodes.push(table);
        continue;
      }
      const item = mdListItem(line);
      if (item) {
        const block = [];
        const baseIndent = mdIndent(line);
        while (i < lines.length && (mdListItem(lines[i]) || lines[i].trim())) {
          if (!mdListItem(lines[i]) && mdIndent(lines[i]) <= baseIndent) break;
          block.push(lines[i]);
          i += 1;
        }
        nodes.push(mdList(block, baseIndent));
        continue;
      }
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !mdListItem(lines[i]) &&
        !/^(#{1,6})\s|^\s*(```|~~~|>)/.test(lines[i])
      ) {
        para.push(lines[i].trim());
        i += 1;
      }
      nodes.push(mdParagraph(para));
    }
    return nodes;
  }

  /** Build one list from *lines*, recursing for anything indented deeper. */
  function mdList(lines, baseIndent) {
    const first = mdListItem(lines[0]) || { ordered: false };
    const list = document.createElement(first.ordered ? "ol" : "ul");
    let current = null;
    let nested = [];
    const flush = () => {
      if (!current || !nested.length) {
        nested = [];
        return;
      }
      if (mdListItem(nested[0])) {
        current.append(mdList(nested, mdIndent(nested[0])));
      } else {
        // An indented continuation: a paragraph or code block under the item.
        for (const node of mdBlocks(nested.join("\n"))) current.append(node);
      }
      nested = [];
    };
    for (const line of lines) {
      const item = mdListItem(line);
      if (item && mdIndent(line) <= baseIndent) {
        flush();
        current = document.createElement("li");
        mdInline(item.text, current);
        list.append(current);
      } else if (current) {
        nested.push(line);
      }
    }
    flush();
    return list;
  }

  function renderMarkdown(text, container) {
    container.textContent = "";
    for (const node of mdBlocks(text)) container.append(node);
  }

  /* -------------------------------------------------------------- chat */

  const CHAT_WIDTH_DEFAULT = 360;
  const CHAT_WIDTH_MIN = 280;

  /** Widen or narrow the drawer, within what the window can spare. */
  function applyChatWidth(px, persist) {
    const max = Math.max(CHAT_WIDTH_MIN, Math.round(window.innerWidth * 0.8));
    const width = Math.round(Math.min(max, Math.max(CHAT_WIDTH_MIN, px)));
    document.documentElement.style.setProperty("--chat-width", width + "px");
    state.chatWidth = width;
    if (dom.chatResizer) {
      dom.chatResizer.setAttribute("aria-valuenow", String(width));
    }
    if (persist) {
      try {
        localStorage.setItem("fcli-chat-width", String(width));
      } catch (_err) {
        /* storage may be unavailable */
      }
    }
  }

  /* ---------------------------------------------------- side pane width */

  const SIDE_WIDTH_DEFAULT = 260;
  const SIDE_WIDTH_MIN = 180;
  let sideWidth = SIDE_WIDTH_DEFAULT;

  /** Set the side pane's width, kept between a readable minimum and half the window. */
  function applySideWidth(px, persist) {
    const max = Math.max(SIDE_WIDTH_MIN, Math.round(window.innerWidth * 0.5));
    sideWidth = Math.round(Math.min(max, Math.max(SIDE_WIDTH_MIN, px)));
    document.documentElement.style.setProperty("--side-width", sideWidth + "px");
    if (dom.sideResizer) dom.sideResizer.setAttribute("aria-valuenow", String(sideWidth));
    if (persist) {
      try {
        localStorage.setItem("fcli-side-width", String(sideWidth));
      } catch (_err) {
        /* storage may be unavailable */
      }
    }
  }

  function initSideResize() {
    let stored = NaN;
    try {
      stored = parseInt(localStorage.getItem("fcli-side-width"), 10);
    } catch (_err) {
      /* storage may be unavailable */
    }
    applySideWidth(stored > 0 ? stored : SIDE_WIDTH_DEFAULT, false);
    const resizer = dom.sideResizer;
    if (!resizer) return;
    const sidebar = resizer.parentElement;

    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      resizer.setPointerCapture(event.pointerId);
      sidebar.classList.add("is-widening");
      const left = sidebar.getBoundingClientRect().left;
      const onMove = (move) => applySideWidth(move.clientX - left, false);
      const onUp = () => {
        resizer.removeEventListener("pointermove", onMove);
        resizer.removeEventListener("pointerup", onUp);
        resizer.removeEventListener("pointercancel", onUp);
        sidebar.classList.remove("is-widening");
        applySideWidth(sideWidth, true);
        // The topology fits itself to the space it has.
        if (state.topoFit) applyTopoZoom();
      };
      resizer.addEventListener("pointermove", onMove);
      resizer.addEventListener("pointerup", onUp);
      resizer.addEventListener("pointercancel", onUp);
    });

    resizer.addEventListener("dblclick", () => applySideWidth(SIDE_WIDTH_DEFAULT, true));

    resizer.addEventListener("keydown", (event) => {
      let next = sideWidth;
      if (event.key === "ArrowRight") next += 20;
      else if (event.key === "ArrowLeft") next -= 20;
      else if (event.key === "Home") next = SIDE_WIDTH_MIN;
      else if (event.key === "End") next = window.innerWidth * 0.5;
      else return;
      event.preventDefault();
      applySideWidth(next, true);
    });

    window.addEventListener("resize", () => applySideWidth(sideWidth, false));
  }

  function initChatResize() {
    let stored = NaN;
    try {
      stored = parseInt(localStorage.getItem("fcli-chat-width"), 10);
    } catch (_err) {
      /* storage may be unavailable */
    }
    applyChatWidth(stored > 0 ? stored : CHAT_WIDTH_DEFAULT, false);
    const resizer = dom.chatResizer;
    if (!resizer) return;

    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      resizer.setPointerCapture(event.pointerId);
      dom.chatDrawer.classList.add("is-resizing");
      const right = dom.chatDrawer.getBoundingClientRect().right;
      const onMove = (move) => applyChatWidth(right - move.clientX, false);
      const onUp = () => {
        resizer.removeEventListener("pointermove", onMove);
        resizer.removeEventListener("pointerup", onUp);
        resizer.removeEventListener("pointercancel", onUp);
        dom.chatDrawer.classList.remove("is-resizing");
        applyChatWidth(state.chatWidth, true);
      };
      resizer.addEventListener("pointermove", onMove);
      resizer.addEventListener("pointerup", onUp);
      resizer.addEventListener("pointercancel", onUp);
    });

    resizer.addEventListener("dblclick", () =>
      applyChatWidth(CHAT_WIDTH_DEFAULT, true)
    );

    resizer.addEventListener("keydown", (event) => {
      let next = state.chatWidth;
      if (event.key === "ArrowLeft") next += 40;
      else if (event.key === "ArrowRight") next -= 40;
      else if (event.key === "Home") next = CHAT_WIDTH_MIN;
      else if (event.key === "End") next = window.innerWidth * 0.8;
      else return;
      event.preventDefault();
      applyChatWidth(next, true);
    });

    window.addEventListener("resize", () => applyChatWidth(state.chatWidth, false));
  }

  function openChat() {
    if (!state.chatEnabled || !dom.chatDrawer) return;
    dom.chatDrawer.hidden = false;
    if (dom.chatInput) dom.chatInput.focus();
  }

  function closeChat() {
    if (state.chatAbort) {
      state.chatAbort.abort();
      state.chatAbort = null;
    }
    if (dom.chatDrawer) dom.chatDrawer.hidden = true;
    setChatBusy(false);
  }

  function setChatBusy(busy) {
    state.chatBusy = busy;
    if (dom.chatSend) {
      // Send doubles as Stop: a reasoning round can run for a while.
      dom.chatSend.textContent = busy ? "Stop" : "Send";
      dom.chatSend.classList.toggle("stop", busy);
      dom.chatSend.title = busy ? "Stop this answer" : "";
    }
    if (dom.chatInput) dom.chatInput.disabled = busy;
    if (dom.chatProvider) dom.chatProvider.disabled = busy;
    if (dom.chatEffort) dom.chatEffort.disabled = busy;
  }

  function activeChatProvider() {
    return state.chatProviders.find((p) => p.id === state.chatProvider) || null;
  }

  function renderChatEfforts() {
    const select = dom.chatEffort;
    if (!select) return;
    const provider = activeChatProvider();
    const efforts = (provider && provider.efforts) || [];
    select.textContent = "";
    if (!efforts.length) {
      select.hidden = true;
      state.chatEffort = null;
      return;
    }
    const auto = document.createElement("option");
    // "auto" leaves the effort out of the request, so the model's own default
    // applies: medium on GPT-5.6, high on Claude and Grok.
    auto.value = "auto";
    auto.textContent = "auto";
    select.append(auto);
    for (const effort of efforts) {
      const option = document.createElement("option");
      option.value = effort;
      option.textContent = effort;
      select.append(option);
    }
    let saved = null;
    try {
      saved = localStorage.getItem("fcli-chat-effort-" + provider.id);
    } catch (_err) {}
    const chosen =
      (saved && (saved === "auto" || efforts.includes(saved)) && saved) ||
      provider.effort ||
      "auto";
    select.value = chosen;
    state.chatEffort = chosen;
    select.hidden = false;
    select.title = "Reasoning effort";
  }

  function renderChatProviders(providers) {
    state.chatProviders = Array.isArray(providers) ? providers : [];
    const select = dom.chatProvider;
    if (!select) return;
    let saved = null;
    try {
      saved = localStorage.getItem("fcli-chat-provider");
    } catch (_err) {}
    const ids = state.chatProviders.map((p) => p.id);
    const preset = state.chatProviders.find((p) => p.default);
    const chosen =
      (saved && ids.includes(saved) && saved) ||
      (preset && preset.id) ||
      ids[0] ||
      null;
    state.chatProvider = chosen;
    select.textContent = "";
    for (const provider of state.chatProviders) {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label || provider.id;
      if (provider.model) option.title = provider.model;
      select.append(option);
    }
    if (chosen) select.value = chosen;
    // With a single key configured there is nothing to pick.
    select.hidden = state.chatProviders.length < 2;
    const active = activeChatProvider();
    select.title = active && active.model ? "Model: " + active.model : "";
    renderChatEfforts();
  }

  function chatContext() {
    const ctx = {};
    if (state.report) ctx.report = state.report.name;
    const inv = dom.invFilter.value.trim();
    if (inv) ctx.inv_filter = inv;
    if (state.topoSelection && state.topoSelection.kind === "node") {
      ctx.topo_node = state.topoSelection.id;
    }
    return ctx;
  }

  function appendChat(role, text) {
    const row = document.createElement("div");
    row.className = "chat-msg " + role;
    if (role === "assistant") {
      const tools = document.createElement("div");
      tools.className = "chat-tools";
      const status = document.createElement("div");
      status.className = "chat-status";
      status.hidden = true;
      const dot = document.createElement("span");
      dot.className = "dot";
      const label = document.createElement("span");
      label.className = "chat-status-text";
      const clock = document.createElement("span");
      clock.className = "chat-status-time";
      status.append(dot, label, clock);
      const body = document.createElement("div");
      body.className = "chat-body";
      // An answer arrives token by token, so the stream fills this in through
      // row._body; the bubble starts empty and *text* is for the user role.
      row.append(tools, status, body);
      row._tools = tools;
      row._status = status;
      row._statusText = label;
      row._statusTime = clock;
      row._body = body;
      row._chips = new Map();
    } else {
      row.textContent = text || "";
    }
    dom.chatLog.append(row);
    scrollChatToEnd();
    return row;
  }

  function scrollChatToEnd() {
    if (dom.chatLog) dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
  }

  /** Show what the agent is doing right now, with a clock since it started. */
  function setChatStatus(row, text) {
    if (!row || !row._status) return;
    stopChatClock(row);
    if (!text) {
      row._status.hidden = true;
      return;
    }
    row._status.hidden = false;
    row._statusText.textContent = text;
    row._statusTime.textContent = "";
    const started = Date.now();
    const tick = () => {
      const seconds = Math.round((Date.now() - started) / 1000);
      row._statusTime.textContent = seconds >= 1 ? seconds + "s" : "";
    };
    row._clock = setInterval(tick, 1000);
    scrollChatToEnd();
  }

  function stopChatClock(row) {
    if (row && row._clock) {
      clearInterval(row._clock);
      row._clock = null;
    }
  }

  function chipLabel(name, args) {
    if (!args) return name;
    // Two details at most: which node, and what was asked of it.
    const parts = [
      args.node || args.inv_filter,
      args.area || args.command || args.path,
    ].filter(Boolean);
    return parts.length ? `${name} ${parts.join(" ")}` : name;
  }

  function addChatChip(row, call) {
    if (!row || !row._tools) return;
    const chip = document.createElement("span");
    chip.className = "chat-chip running";
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.textContent = chipLabel(call.name || "tool", call.args);
    const meta = document.createElement("span");
    meta.className = "chat-chip-meta";
    chip.append(dot, label, meta);
    chip._meta = meta;
    row._tools.append(chip);
    if (call.id) row._chips.set(call.id, chip);
    scrollChatToEnd();
    return chip;
  }

  function finishChatChip(row, result) {
    if (!row || !row._chips) return;
    const chip = row._chips.get(result.id);
    if (!chip) return;
    chip.classList.remove("running");
    chip.classList.add(result.error ? "failed" : "ok");
    const ms = Number(result.ms) || 0;
    chip._meta.textContent = result.repeat
      ? "repeat"
      : ms >= 1000
        ? (ms / 1000).toFixed(1) + "s"
        : ms + "ms";
    if (result.error) chip.title = result.error;
    else if (result.repeat) chip.title = "identical call, reused";
  }

  /** A muted line in the bubble for things that are not the answer or an error. */
  function addChatNote(row, text) {
    if (!row || !row._body) return;
    const note = document.createElement("div");
    note.className = "chat-note";
    note.textContent = text;
    row.insertBefore(note, row._body);
    scrollChatToEnd();
  }

  async function readChatSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message";
        const dataLines = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7).trim();
          else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
        }
        if (!dataLines.length) continue;
        let payload = {};
        try {
          payload = JSON.parse(dataLines.join("\n"));
        } catch (_err) {
          payload = { text: dataLines.join("\n") };
        }
        onEvent(event, payload);
      }
    }
  }

  // One click for the question every session starts with. The agent is told
  // in its system prompt to answer it from the incidents and the timeline.
  const TRIAGE_PROMPT =
    "Triage the fabric: what is wrong, what changed recently, the most likely root cause, " +
    "and what I should check next. Start from the incidents and the recent changes.";

  function triage() {
    if (!state.chatEnabled || state.chatBusy) return;
    dom.chatInput.value = TRIAGE_PROMPT;
    sendChat();
  }

  async function sendChat(event) {
    if (event) event.preventDefault();
    if (!state.chatEnabled) return;
    if (state.chatBusy) {
      if (state.chatAbort) state.chatAbort.abort();
      return;
    }
    const text = (dom.chatInput.value || "").trim();
    if (!text) return;
    dom.chatInput.value = "";
    state.chatMessages.push({ role: "user", content: text });
    appendChat("user", text);
    const bubble = appendChat("assistant");
    setChatStatus(bubble, "Thinking…");
    setChatBusy(true);
    const controller = new AbortController();
    state.chatAbort = controller;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          messages: state.chatMessages,
          context: chatContext(),
          provider: state.chatProvider || undefined,
          effort: state.chatEffort || undefined,
        }),
      });
      if (!res.ok) {
        let detail = res.statusText;
        try {
          const payload = await res.json();
          if (payload && payload.error) detail = payload.error;
        } catch (_err) {}
        throw new Error(detail);
      }
      let reply = "";
      await readChatSse(res, (kind, payload) => {
        if (kind === "start") {
          const where = [
            payload.provider,
            payload.model,
            payload.effort && "effort " + payload.effort,
          ]
            .filter(Boolean)
            .join(" · ");
          bubble.title = where;
          setChatStatus(bubble, "Thinking…");
        } else if (kind === "token" && payload.text) {
          // One event per round: keep rounds apart as paragraphs, or a
          // preamble runs into the answer that follows it.
          reply += (reply ? "\n\n" : "") + payload.text;
          if (bubble._body) renderMarkdown(reply, bubble._body);
          scrollChatToEnd();
        } else if (kind === "tool") {
          addChatChip(bubble, payload);
          const round = payload.round
            ? ` (round ${payload.round}/${payload.rounds})`
            : "";
          setChatStatus(
            bubble,
            "Running " +
              chipLabel(payload.name || "tool", payload.args) +
              "…" +
              round
          );
        } else if (kind === "tool_result") {
          finishChatChip(bubble, payload);
          setChatStatus(bubble, "Thinking…");
        } else if (kind === "notice") {
          addChatNote(bubble, payload.text || "");
        } else if (kind === "done") {
          setChatStatus(bubble, "");
        } else if (kind === "error") {
          throw new Error(payload.error || "chat failed");
        }
      });
      if (reply) state.chatMessages.push({ role: "assistant", content: reply });
    } catch (err) {
      const aborted = err && err.name === "AbortError";
      const message = aborted ? "Stopped." : (err && err.message) || String(err);
      if (!aborted) bubble.classList.add("error");
      if (bubble._body) {
        // Keep whatever the model already said and add the reason below it.
        const note = document.createElement("p");
        note.textContent = message;
        bubble._body.append(note);
      } else {
        bubble.textContent = message;
      }
    } finally {
      setChatStatus(bubble, "");
      state.chatAbort = null;
      setChatBusy(false);
      if (dom.chatInput) dom.chatInput.focus();
    }
  }

  initChatResize();
  initSideResize();

  if (dom.chatOpen) dom.chatOpen.addEventListener("click", openChat);
  if (dom.chatClose) dom.chatClose.addEventListener("click", closeChat);
  if (dom.chatProvider) {
    dom.chatProvider.addEventListener("change", () => {
      state.chatProvider = dom.chatProvider.value || null;
      const active = activeChatProvider();
      dom.chatProvider.title = active && active.model ? "Model: " + active.model : "";
      renderChatEfforts();
      try {
        if (state.chatProvider) {
          localStorage.setItem("fcli-chat-provider", state.chatProvider);
        }
      } catch (_err) {}
    });
  }
  if (dom.chatEffort) {
    dom.chatEffort.addEventListener("change", () => {
      state.chatEffort = dom.chatEffort.value || null;
      try {
        if (state.chatProvider && state.chatEffort) {
          localStorage.setItem(
            "fcli-chat-effort-" + state.chatProvider,
            state.chatEffort
          );
        }
      } catch (_err) {}
    });
  }
  if (dom.chatForm) dom.chatForm.addEventListener("submit", sendChat);
  if (dom.chatInput) {
    dom.chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        sendChat(event);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && dom.chatDrawer && !dom.chatDrawer.hidden) {
      closeChat();
    }
  });

  loadReports();
  loadInventory();
  // Fast enough for the transfer mark to track gNMI activity; inventory is
  // served from memory, so this is cheap.
  setInterval(loadInventory, 1000);
})();
