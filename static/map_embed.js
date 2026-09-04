/**
 * 单页地图视图：与列表共用 window.queryParams / openDetail / jget
 */
(function () {
  let map = null;
  let markers = [];
  let loadTimer = null;
  let placeSearch = null;
  let ready = false;
  let loadingSdk = null;
  let lastCluster = null;
  let items = [];

  function money(n) {
    if (n == null || n === "") return "—";
    const v = Number(n);
    if (Number.isNaN(v)) return "—";
    if (v >= 10000) return (v / 10000).toFixed(1) + "亿";
    if (v >= 1000) return (v / 1000).toFixed(1) + "千万";
    return Math.round(v) + "万";
  }

  function fmtCnt(n) {
    n = Number(n) || 0;
    if (n >= 10000) return (n / 10000).toFixed(1) + "万家";
    return n + "家";
  }

  function bubbleSize(cnt) {
    if (cnt >= 80) return "lg";
    if (cnt <= 15) return "sm";
    return "";
  }

  function mapQuery(extra = {}) {
    const p = typeof window.queryParams === "function"
      ? window.queryParams()
      : new URLSearchParams();
    Object.entries(extra).forEach(([k, v]) => {
      if (v == null || v === "") p.delete(k);
      else p.set(k, v);
    });
    return p;
  }

  function clearMarkers() {
    if (map && markers.length) {
      map.remove(markers);
      markers = [];
    }
  }

  function renderClusters(data) {
    clearMarkers();
    const level = data.level;
    const list = data.clusters || [];
    const el = document.getElementById("viewportCount");
    if (el) {
      el.innerHTML =
        `可视区域内 <b>${(data.total || 0).toLocaleString("zh-CN")}</b> 家已定位` +
        `<div class="sub">层级：${
          level === "city" ? "城市" :
          level === "district" ? "区县" :
          level === "street" ? "街道" : "地址"
        } · ${list.length} 个气泡 · 与列表同一筛选</div>`;
    }

    list.forEach((c) => {
      const lng = Number(c.lng);
      const lat = Number(c.lat);
      if (!lng || !lat) return;
      let node;
      if (level === "city" || level === "district") {
        node = document.createElement("div");
        node.className = "map-bubble " + bubbleSize(c.cnt);
        node.innerHTML =
          `<div class="bn">${c.name || c.district || c.city || ""}</div>` +
          `<div class="bm">${fmtCnt(c.cnt)}</div>` +
          `<div class="bc">均资 ${money(c.metric)}</div>`;
      } else {
        node = document.createElement("div");
        node.className = "map-pill";
        const label = String(c.name || "").replace(/(.{8}).+/, "$1…");
        node.innerHTML =
          `<span class="pn">${label}</span><span>${c.cnt}家</span><span>${money(c.metric)}</span>`;
      }
      const m = new AMap.Marker({
        position: [lng, lat],
        content: node,
        offset:
          level === "city" || level === "district"
            ? new AMap.Pixel(-39, -39)
            : new AMap.Pixel(-70, -14),
        zIndex: 100 + Math.min(Number(c.cnt) || 0, 50),
      });
      m.on("click", () => onClusterClick(c, level, [lng, lat]));
      markers.push(m);
    });
    if (markers.length) map.add(markers);
  }

  async function loadClusters() {
    if (!map || !ready) return;
    const b = map.getBounds();
    if (!b) return;
    const sw = b.getSouthWest();
    const ne = b.getNorthEast();
    const zoom = map.getZoom();
    // 聚合看视野：不锁死点选的区/街（否则缩回会只剩一泡）
    const params = mapQuery();
    params.delete("district");
    params.delete("street");
    params.set("min_lng", sw.getLng());
    params.set("min_lat", sw.getLat());
    params.set("max_lng", ne.getLng());
    params.set("max_lat", ne.getLat());
    params.set("zoom", zoom.toFixed(1));

    try {
      const data = await window.jget(`/api/map/clusters?${params}`);
      renderClusters(data);
    } catch (e) {
      const el = document.getElementById("viewportCount");
      if (el) {
        el.innerHTML =
          `加载失败：${e.message}<div class="sub">视野过大时请先放大到城市级</div>`;
      }
    }
  }

  function scheduleLoad() {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadClusters, 280);
  }

  function setFilterField(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    const v = value || "";
    if (el.tagName === "SELECT") {
      if (v && ![...el.options].some((o) => o.value === v)) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v;
        el.appendChild(o);
      }
      el.value = v;
    } else {
      el.value = v;
    }
  }

  async function ensureDistrictOptions(city, district) {
    if (!city || !district) return;
    const dist = document.getElementById("district");
    if (!dist) return;
    if (![...dist.options].some((o) => o.value === district)) {
      try {
        const p = (document.getElementById("province") || {}).value || "";
        const rows = await window.jget(
          `/api/districts?province=${encodeURIComponent(p)}&city=${encodeURIComponent(city)}`
        );
        dist.innerHTML = '<option value="">全部</option>';
        (rows || []).forEach((r) => {
          const o = document.createElement("option");
          o.value = r.k;
          o.textContent = `${r.k}（${r.n}）`;
          dist.appendChild(o);
        });
      } catch (_) {
        const o = document.createElement("option");
        o.value = district;
        o.textContent = district;
        dist.appendChild(o);
      }
    }
    dist.value = district;
  }

  async function ensureStreetOptions(city, district, street) {
    if (!street) return;
    const sel = document.getElementById("street");
    if (!sel) return;
    if (![...sel.options].some((o) => o.value === street)) {
      try {
        const p = (document.getElementById("province") || {}).value || "";
        const rows = await window.jget(
          `/api/streets?province=${encodeURIComponent(p)}&city=${encodeURIComponent(city || "")}&district=${encodeURIComponent(district || "")}`
        );
        sel.innerHTML = '<option value="">全部</option>';
        (rows || []).forEach((r) => {
          const o = document.createElement("option");
          o.value = r.k;
          o.textContent = `${r.k}（${r.n}）`;
          sel.appendChild(o);
        });
      } catch (_) {
        const o = document.createElement("option");
        o.value = street;
        o.textContent = street;
        sel.appendChild(o);
      }
    }
    sel.value = street;
  }

  async function onClusterClick(c, level, pos) {
    lastCluster = { c, level, pos };
    const panel = document.getElementById("mapClusterPanel");
    if (panel) panel.classList.add("open");
    document.getElementById("mapClusterTitle").textContent =
      c.name || c.district || "区域线索";
    document.getElementById("mapClusterSub").textContent =
      `${c.city || ""} ${c.district || ""} · ${fmtCnt(c.cnt)} · 均资 ${money(c.metric)}`;
    document.getElementById("mapClusterList").innerHTML =
      '<div class="map-empty">加载中…</div>';

    if (level === "city" || level === "district") {
      map.setZoomAndCenter(
        level === "city" ? 12 : Math.max(map.getZoom(), 13.2),
        pos
      );
    }

    if (c.city) setFilterField("city", c.city);
    if (level === "city") {
      setFilterField("district", "");
      setFilterField("street", "");
    } else if (level === "district") {
      await ensureDistrictOptions(c.city, c.district);
      setFilterField("street", "");
    } else if (level === "street") {
      await ensureDistrictOptions(c.city, c.district);
      if (c.street) await ensureStreetOptions(c.city, c.district, c.street);
      else setFilterField("street", "");
    }

    if (typeof window.syncAppUrl === "function") window.syncAppUrl();

    const params = mapQuery();
    params.set("limit", "200");
    if (level === "grid") {
      const pad = 0.002;
      params.set("min_lng", pos[0] - pad);
      params.set("min_lat", pos[1] - pad);
      params.set("max_lng", pos[0] + pad);
      params.set("max_lat", pos[1] + pad);
    }

    try {
      const data = await window.jget(`/api/map/companies?${params}`);
      items = data.items || [];
      const more =
        data.total > items.length ? ` · 展示前 ${items.length} 条` : "";
      document.getElementById("mapClusterSub").textContent =
        `${c.city || ""} ${c.district || c.name || ""} · 共 ${data.total} 家${more}`;
      if (!items.length) {
        document.getElementById("mapClusterList").innerHTML =
          '<div class="map-empty">该区域暂无已地理编码企业</div>';
        return;
      }
      document.getElementById("mapClusterList").innerHTML = items
        .map(
          (r) => `
        <div class="map-item" data-code="${r.credit_code || ""}" data-lng="${r.lng || ""}" data-lat="${r.lat || ""}">
          <div class="name">${r.company_name || ""}</div>
          <div class="meta">${[r.district, r.street].filter(Boolean).join(" · ") || r.address || ""}</div>
          <div class="meta">${r.industry_l1 || "—"} · ${money(r.capital_wan)}
            ${r.has_mobile ? '<span class="tag">手机</span>' : ""}
          </div>
        </div>`
        )
        .join("");
    } catch (e) {
      document.getElementById("mapClusterList").innerHTML =
        `<div class="map-empty">加载失败：${e.message}</div>`;
    }
  }

  async function loadAmapSdk() {
    if (window.AMap) return;
    if (loadingSdk) return loadingSdk;
    loadingSdk = (async () => {
      const cfg = await window.jget("/api/amap_config");
      if (!cfg.js_key) throw new Error("未配置高德 JS Key");
      window._AMapSecurityConfig = { securityJsCode: cfg.security_code || "" };
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = `https://webapi.amap.com/maps?v=2.0&key=${cfg.js_key}&plugin=AMap.PlaceSearch,AMap.AutoComplete`;
        s.onload = resolve;
        s.onerror = () => reject(new Error("高德脚本加载失败"));
        document.head.appendChild(s);
      });
    })();
    return loadingSdk;
  }

  async function ensureInit() {
    if (ready) {
      setTimeout(() => map && map.resize && map.resize(), 60);
      scheduleLoad();
      return;
    }
    const host = document.getElementById("amap");
    if (!host) throw new Error("地图容器缺失");
    document.getElementById("viewportCount").textContent = "正在加载地图…";
    await loadAmapSdk();

    const city = (document.getElementById("city") || {}).value || "苏州市";
    map = new AMap.Map("amap", {
      zoom: 11,
      center: [120.73, 31.32],
      viewMode: "2D",
      mapStyle: "amap://styles/whitesmoke",
    });
    map.on("moveend", scheduleLoad);
    map.on("zoomend", scheduleLoad);
    ready = true;

    if (city) {
      map.setCity(city, () => scheduleLoad());
    } else {
      scheduleLoad();
    }
  }

  function flyToFilterCity() {
    if (!map) return;
    const city = (document.getElementById("city") || {}).value;
    if (city) map.setCity(city, () => scheduleLoad());
    else scheduleLoad();
  }

  function searchPlace() {
    const q = (document.getElementById("mapPlaceQ") || {}).value.trim();
    if (!q || !map) return;
    const city = (document.getElementById("city") || {}).value || "全国";
    if (!placeSearch) placeSearch = new AMap.PlaceSearch({ city });
    placeSearch.setCity(city);
    placeSearch.search(q, (status, result) => {
      if (status !== "complete" || !result.poiList || !result.poiList.pois.length) {
        alert("未找到该地点");
        return;
      }
      const poi = result.poiList.pois[0];
      map.setZoomAndCenter(15, [poi.location.lng, poi.location.lat]);
    });
  }

  function bindUi() {
    const close = document.getElementById("btnCloseMapCluster");
    if (close) {
      close.onclick = () =>
        document.getElementById("mapClusterPanel").classList.remove("open");
    }
    const zoom = document.getElementById("btnMapZoomIn");
    if (zoom) {
      zoom.onclick = () => {
        if (lastCluster)
          map.setZoomAndCenter(Math.min(map.getZoom() + 2, 17), lastCluster.pos);
      };
    }
    const goList = document.getElementById("btnMapToList");
    if (goList) {
      goList.onclick = () => {
        if (typeof window.setAppView === "function") window.setAppView("list");
        if (typeof window.search === "function") window.search().catch(alert);
      };
    }
    const placeBtn = document.getElementById("btnMapPlace");
    if (placeBtn) placeBtn.onclick = () => searchPlace();
    const placeQ = document.getElementById("mapPlaceQ");
    if (placeQ) {
      placeQ.onkeydown = (e) => {
        if (e.key === "Enter") searchPlace();
      };
    }
    const list = document.getElementById("mapClusterList");
    if (list) {
      list.onclick = (e) => {
        const item = e.target.closest(".map-item");
        if (!item) return;
        const code = item.dataset.code;
        const lng = Number(item.dataset.lng);
        const lat = Number(item.dataset.lat);
        if (lng && lat) map.setZoomAndCenter(17, [lng, lat]);
        if (code && typeof window.openDetail === "function") {
          window.openDetail(code).catch(alert);
        }
      };
    }
    const hideHowto = document.getElementById("btnHideMapHowto");
    if (hideHowto) {
      hideHowto.onclick = () => {
        document.getElementById("mapHowto").classList.add("hidden");
        localStorage.setItem("map_howto_hidden", "1");
      };
    }
    if (localStorage.getItem("map_howto_hidden") === "1") {
      const h = document.getElementById("mapHowto");
      if (h) h.classList.add("hidden");
    }
  }

  window.MapView = {
    ensureInit,
    reload: () => {
      flyToFilterCity();
    },
    scheduleLoad,
    isReady: () => ready,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindUi);
  } else {
    bindUi();
  }
})();
