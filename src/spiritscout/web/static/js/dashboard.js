"use strict";
/* 总览只有观察与跳转操作；所有时间明确以 Asia/Shanghai 显示。 */
const Dashboard = (() => {
  const state={period:"today",metric:"tokens",dimension:"model",query:"",mode:"all",status:"all",sort:"activity",cacheQuery:""};
  let current=null, root=null;
  const periods={hour:I18n.t("最近一小时"),today:I18n.t("今天"), "7d":I18n.t("近七天"), "30d":I18n.t("近三十天"),all:I18n.t("全部记录")};
  const phases={planning:I18n.t("灵枢规划中"),queued:I18n.t("等待调度"),starting:I18n.t("准备启动"),model_preflight:I18n.t("检查模型"),platform_profile:I18n.t("读取平台规则"),runtime_starting:I18n.t("初始化工具"),executing:I18n.t("执行中"),master_reply:I18n.t("灵枢回复中"),scope_confirmation:I18n.t("等待范围确认"),intent_routing:I18n.t("理解任务"),mode_routing:I18n.t("确认模式")};
  const num=n=>Number(n || 0).toLocaleString(I18n.dateLocale);
  const compact=n=>n>=1e6 ? (n/1e6).toFixed(2)+"M" : n>=1000 ? (n/1000).toFixed(1)+"k" : num(n);
  const percent=n=>n == null ? I18n.t("未提供") : (n*100).toFixed(1)+"%";
  const clock=(ts,day=false)=>ts ? new Intl.DateTimeFormat(I18n.dateLocale,{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",...(day ? {} : {hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false})}).format(new Date(ts*1000)) : I18n.t("未记录");
  const money=(c,precise=false)=>{
    if(!c) return I18n.t("未知");
    const parts=[], amount=(n,digits)=>precise ? n.toLocaleString(I18n.dateLocale,{minimumFractionDigits:2,maximumFractionDigits:8}) : n.toFixed(digits);
    if(c.cny_min != null) parts.push(c.cny_min === c.cny_max ? `¥${amount(c.cny_min,2)}` : `¥${amount(c.cny_min,2)}–${amount(c.cny_max,2)}`);
    if(c.usd != null) parts.push(`$${amount(c.usd,3)}`);
    return parts.join(" · ") || I18n.t("未知");
  };
  const coverage=m=>m?.calls ? I18n.t`${m.cost?.priced_calls || 0}/${m.calls} 次调用有计价${m.cost?.unpriced_calls ? I18n.t`，${m.cost.unpriced_calls} 次未知` : ""}` : I18n.t("本时间范围暂无已返回调用");
  const empty=text=>`<p class="dash-empty">${esc(text)}</p>`;
  const jump=(s,text,extra="")=>`<button type="button" class="dash-link" data-dash-open="${esc(s.session_id)}" ${extra}>${esc(text || s.title || s.session_title)}</button>`;
  const outcome=o=>o ? I18n.t`${o.solved == null ? "" : I18n.t`已解 ${num(o.solved)} / 未解 ${num(o.unsolved)} · `}Flag 通过 ${num(o.flags_accepted)} / 提取 ${num(o.flags_extracted)}${o.confirmed_vulns ? I18n.t` · 确认漏洞 ${num(o.confirmed_vulns)}` : ""}${o.score != null ? I18n.t` · 平台得分 ${num(o.score)}` : ""}` : I18n.t("暂无成果记录");
  function tooltipContent(b) {
    return I18n.html`<strong>${esc(b.label)}（北京时间）</strong><span>费用估算：${money(b.cost,true)}</span><span>Token：${num(b.tokens)}</span><span>输入缓存命中率：${percent(b.cache?.hit_rate)}</span><span>调用：${num(b.calls)} 次${b.results?.length ? I18n.t` · 确认成果：${num(b.results.length)} 项` : ""}</span>`;
  }
  function hideChartTip() {const tip=root?.querySelector(".dash-chart-tooltip");if(tip)tip.hidden=true;}
  function showChartTip(event) {
    const group=event.target.closest?.("[data-dash-bucket]"), tip=root?.querySelector(".dash-chart-tooltip");
    if(!group || !tip)return;
    const index=group.dataset.dashBucket, bucket=current?.trend[index];if(!bucket)return;
    if(!tip.hidden && tip.dataset.bucket === index)return;
    const anchor=group.querySelector(".dash-chart-bar, .dash-cache-point, .dash-result-dot") || group;
    const bounds=anchor.getBoundingClientRect(), panel=root.querySelector(".dash-trend").getBoundingClientRect();
    tip.innerHTML=tooltipContent(bucket);tip.hidden=false;tip.dataset.bucket=index;
    const width=tip.offsetWidth;
    tip.style.left=`${Math.max(0,Math.min(panel.width-width,bounds.left+bounds.width/2-panel.left-width/2))}px`;
    tip.style.top=`${bounds.top-panel.top-tip.offsetHeight-10}px`;
  }
  function normalize(data) {
    if(data.summary && data.sessions) return data;
    // 未重启的旧服务仍可展示既有统计，新指标明确标为尚未提供。
    const cost={usd:data.total_cost>0 ? data.total_cost : null,unpriced_calls:0,priced_calls:0};
    return {...data,legacy:true,generated_at:Date.now()/1000,summary:{tokens:data.total_tokens || 0,calls:0,cost,cache:{}},today:null,recent_hour:null,previous_hour:null,
      cache_windows:{},cache_groups:{model:Object.entries(data.per_model || {}).map(([name,tokens])=>({name,tokens,calls:0,cost:{},cache:{}}))},
      trend:Object.entries(data.daily || {}).map(([label,tokens])=>({label,tokens,calls:0,cost:{},cache:{},results:[]})),
      alerts:[],findings:[],outcomes:[],sessions:(S.sessions || []).map(s=>({...s,tasks:{},capacity:{},selected:{tokens:s.tokens,cache:{},cost:{usd:s.cost_usd>0 ? s.cost_usd : null}},latest_progress:I18n.t("后端尚未提供执行摘要"),outcomes:{flags_extracted:s.flag_stats?.total,flags_accepted:s.flag_stats?.accepted}}))};
  }
  function scaffold(box) {
    box.innerHTML=I18n.html`<div class="dash-console">
      <header class="dash-heading"><div><h2>仪表盘</h2><p>用量与执行总览 · 看清模型消耗，追踪测试成果。</p></div><span class="dash-updated" role="status"></span></header>
      <div class="dash-toolbar"><div class="dash-periods" role="group" aria-label="统计时间范围">${Object.entries(periods).map(([key,label])=>`<button type="button" data-dash-period="${key}">${label}</button>`).join("")}</div><span class="dash-range-note">所有消耗指标均跟随所选时段</span></div>
      <p class="dash-notice" role="status" hidden></p>
      <div class="dash-overview"></div>
      <div class="dash-health" role="status"></div>
      <div class="dash-usage-grid"><section class="dash-panel"><header><div><h3>Token 构成</h3><p>输入已包含缓存，输出包含端点计入的推理用量。</p></div></header><div class="dash-composition"></div></section><section class="dash-panel"><header><div><h3>模型消耗排行</h3><p>按所选时段的已记录 Token 排序。</p></div></header><div class="dash-models"></div></section></div>
      <section class="dash-panel dash-trend-panel"><header><div><h3>消耗与成果趋势</h3><p>切换费用、Token 或输入缓存趋势；成果按北京时间标注。</p></div><div class="dash-toggle" role="group" aria-label="趋势指标"><button type="button" data-dash-metric="cost">费用</button><button type="button" data-dash-metric="tokens">Token</button><button type="button" data-dash-metric="cache">缓存命中率</button></div></header><div class="dash-trend"></div></section>
      <div class="dash-live-grid"><section class="dash-panel"><header><h3>正在执行</h3><span class="dash-active-count"></span></header><div class="dash-active"></div></section><section class="dash-panel dash-attention-panel"><header><h3>需要关注</h3><span>提醒不影响任务运行</span></header><div class="dash-attention"></div></section></div>
      <section class="dash-panel"><header><div><h3>缓存效果与消耗构成</h3><p>按已测量的输入 Token 计算命中率。</p></div></header><div class="dash-cache-windows"></div>
        <div class="dash-cache-controls"><div class="dash-toggle" role="group" aria-label="缓存统计维度"><button type="button" data-dash-dimension="model">按模型</button><button type="button" data-dash-dimension="actor">按角色</button><button type="button" data-dash-dimension="session_id">按会话</button></div><label>查找分组<input data-dash-field="cacheQuery" type="search" placeholder="模型、角色或会话名称"></label></div><div class="dash-cache-table dash-table-scroll"></div>
      </section>
      <section class="dash-panel"><header><div><h3>会话明细</h3><p>成果为累计值，消耗按所选时间范围统计。</p></div></header>
        <div class="dash-filters"><label>查找会话<input data-dash-field="query" type="search" placeholder="名称或会话 ID"></label>
        <label>模式<select data-dash-field="mode"><option value="all">全部模式</option><option value="src">SRC</option><option value="ctf">CTF</option><option value="competition">智能体</option></select></label>
        <label>状态<select data-dash-field="status"><option value="all">全部状态</option><option value="running">运行中</option><option value="paused">已暂停</option><option value="completed">已完成</option><option value="failed">失败</option><option value="interrupted">已中断</option><option value="stopped">已停止</option><option value="created">排队 / 已创建</option></select></label>
        <label>排序<select data-dash-field="sort"><option value="activity">最近活动</option><option value="result">最近成果</option><option value="cost">近期费用</option><option value="cache">缓存命中率从低到高</option><option value="tokens">Token 消耗</option></select></label></div><div class="dash-sessions-table dash-table-scroll"></div>
      </section>
      <div class="dash-bottom-grid"><section class="dash-panel"><header><h3>跨会话最近发现</h3><span>每条标注来源</span></header><div class="dash-recent-findings"></div></section><section class="dash-panel"><header><h3>经验记录</h3><span class="dash-experience-count"></span></header><div class="dash-experience"></div></section></div>
      <p class="dash-footnote">北京时间 UTC+8 · 金额均为运行记录中的估算，不是供应商账单；不同币种分别展示，不自动换算。缺少计价记录的调用显示为未知。Token 为端点已报告用量之和，含重试及失败 / 取消前已报告的部分；未知用量不估填，历史漏记无法自动补回。</p>
    </div>`;
    box.onclick=event=>{
      const b=event.target.closest("button");if(!b)return;
      if(b.dataset.dashOpen) {openSession(b.dataset.dashOpen);return;}
      if(b.dataset.dashPeriod) {state.period=b.dataset.dashPeriod;root.querySelectorAll("[data-dash-period]").forEach(x=>x.setAttribute("aria-pressed",String(x===b)));renderDash();return;}
      if(b.dataset.dashMetric) state.metric=b.dataset.dashMetric;
      if(b.dataset.dashDimension) state.dimension=b.dataset.dashDimension;
      if(current) paint();
    };
    const update=event=>{
      const field=event.target.dataset.dashField;if(!field)return;
      state[field]=event.target.value;
      if(field === "cacheQuery") cacheTable();else sessionTable();
    };
    box.oninput=update;box.onchange=update;
    box.onpointerover=showChartTip;box.onpointermove=showChartTip;
    box.onfocusin=event=>requestAnimationFrame(()=>{
      if(document.activeElement === event.target)showChartTip(event);
    });
    const leave=event=>{
      const group=event.target.closest?.("[data-dash-bucket]");
      if(group && !group.contains(event.relatedTarget))hideChartTip();
    };
    box.onpointerout=leave;box.onfocusout=leave;
    box.addEventListener("scroll",hideChartTip,true);
    box.addEventListener("keydown",event=>{if(event.key === "Escape")hideChartTip();});
  }
  function trendChart(buckets,metric) {
    if(!buckets.length) return empty(I18n.t("本时间范围暂无逐笔用量记录"));
    const cacheView=metric === "cache", cny=buckets.some(b=>b.cost?.cny_max != null);
    const unit=cacheView ? I18n.t("输入缓存命中率") : metric === "tokens" ? "Token" : cny ? I18n.t("人民币估算 ¥（上界）") : I18n.t("美元估算 $");
    const value=b=>cacheView ? b.cache?.hit_rate : metric === "tokens" ? b.tokens : cny ? b.cost?.cny_max : b.cost?.usd;
    const valid=v=>typeof v === "number" && Number.isFinite(v) && v >= 0;
    const peak=Math.max(...buckets.map(b=>valid(value(b)) ? value(b) : 0));
    const hasData=buckets.some(b=>b.calls>0 || b.tokens>0 || valid(b.cache?.hit_rate) || valid(b.cost?.cny_max) || valid(b.cost?.usd) || b.results?.length);
    if(!hasData) return empty(I18n.t("本时间范围暂无逐笔用量或确认成果记录"));
    // 按实际数量级设置刻度；小额费用不再被固定的 1 元上限压扁。
    const power=peak>0 ? 10**Math.floor(Math.log10(peak)) : 1;
    const max=cacheView ? 1 : peak>0 ? [1,2,5,10].find(n=>n*power>=peak)*power : 1;
    const axis=n=>cacheView ? `${Math.round(n*100)}%` : metric === "tokens" ? compact(n) : n.toFixed(Math.min(6,Math.max(2,1-Math.floor(Math.log10(max)))));
    const left=76,right=28,w=Math.max(640,left+right+buckets.length*68),h=260,top=22,bottom=62, baseline=h-bottom, usable=baseline-top;
    const dx=(w-left-right)/buckets.length, barWidth=Math.min(36,dx*.6);
    let path="",connected=false;
    const marks=buckets.map((b,i)=>{
      const x=left+dx*(i+.5), v=value(b);
      let mark="";
      if(cacheView) {
        if(!valid(v)) connected=false;
        else {
          const y=top+(1-Math.min(v,1))*usable;
          path+=`${connected ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;connected=true;
          mark=`<circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="2.5" class="dash-cache-point"/>`;
        }
      } else if(valid(v) && v>0) {
        const height=Math.max(.75,v/max*usable);
        mark=`<rect x="${(x-barWidth/2).toFixed(2)}" y="${(baseline-height).toFixed(2)}" width="${barWidth.toFixed(2)}" height="${height.toFixed(2)}" class="dash-chart-bar"/>`;
      }
      const title=I18n.t`${b.label} 北京时间 · ${num(b.tokens)} Token · ${money(b.cost)} · 缓存 ${percent(b.cache?.hit_rate)}${b.results?.length ? I18n.t` · ${b.results.length} 项成果` : ""}`;
      return `<g tabindex="0" data-dash-bucket="${i}" aria-label="${esc(title)}" aria-describedby="dash-chart-tooltip">${mark}${b.results?.length ? `<circle cx="${x.toFixed(2)}" cy="${baseline+10}" r="4" class="dash-result-dot"/>` : ""}</g>`;
    }).join("");
    const grid=[0,.25,.5,.75,1].map(r=>{const y=baseline-r*usable;return `<line x1="${left}" y1="${y}" x2="${w-right}" y2="${y}" class="${r ? "dash-chart-grid" : "dash-chart-axis"}"/><text x="${left-10}" y="${y+4}" text-anchor="end">${axis(r*max)}</text>`;}).join("");
    // 每个时间桶都有居中标签；日期、小时分两行，长时间范围横向滚动。
    const ticks=buckets.map((b,i)=>{
      const x=left+dx*(i+.5), [date,hour]=b.label.split(" ");
      return `<text class="dash-chart-tick" x="${x}" y="${baseline+30}" text-anchor="middle"><tspan x="${x}">${esc(date)}</tspan>${hour ? `<tspan x="${x}" dy="14">${esc(hour)}</tspan>` : ""}</text>`;
    }).join("");
    const mixed=metric === "cost" && cny && buckets.some(b=>b.cost?.usd != null);
    return I18n.html`<div class="dash-chart-legend"><span><i class="${cacheView ? "dash-key-cache" : "dash-key-cost"}"></i>${unit}</span><span><i class="dash-key-result"></i>记录到的确认成果</span>${mixed ? I18n.html("<span>另有美元计价，见下方数据表</span>") : ""}</div>
      <div class="dash-chart-viewport" tabindex="0" role="region" aria-label="趋势图，可横向滚动查看每柱时间"><svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" style="min-width:${w}px" role="img" aria-label="${esc(periods[state.period])}${esc(unit)}趋势，时间均为北京时间" class="dash-trend-svg">${grid}${cacheView ? `<path d="${path}" class="dash-cache-path"/>` : ""}${marks}${ticks}</svg></div>
      <div id="dash-chart-tooltip" class="dash-chart-tooltip" role="tooltip" hidden></div>
      <details class="dash-trend-data"><summary>查看趋势数据（北京时间）</summary><div class="dash-table-scroll"><table class="list"><thead><tr><th>时间</th><th>费用估算</th><th>Token</th><th>输入缓存命中率</th><th>确认成果</th></tr></thead><tbody>${buckets.map(b=>`<tr><td>${esc(b.label)}</td><td>${money(b.cost)}</td><td>${num(b.tokens)}</td><td>${percent(b.cache?.hit_rate)}</td><td>${(b.results || []).map(r=>esc(r.label)).join("、") || "—"}</td></tr>`).join("")}</tbody></table></div></details>`;
  }
  function filteredSessions(data) {
    const q=state.query.toLowerCase();
    return data.filter(s=>(!q || `${s.title} ${s.session_id}`.toLowerCase().includes(q)) && (state.mode === "all" || s.mode === state.mode) &&
      (state.status === "all" || (state.status === "paused" ? s.paused : s.status === state.status && !s.paused))).sort((a,b)=>{
      if(state.sort === "result") return (b.last_result || 0)-(a.last_result || 0);
      if(state.sort === "cost") {
        // 不把美元数值当人民币比较；先按币种归组，再比较同币种金额。
        const currency=s=>s.selected?.cost?.cny_max != null ? 2 : s.selected?.cost?.usd != null ? 1 : 0;
        return currency(b)-currency(a) || (b.selected?.cost?.cny_max ?? b.selected?.cost?.usd ?? -1)-(a.selected?.cost?.cny_max ?? a.selected?.cost?.usd ?? -1);
      }
      if(state.sort === "cache") return (a.selected?.cache?.hit_rate ?? 2)-(b.selected?.cache?.hit_rate ?? 2);
      if(state.sort === "tokens") return (b.selected?.tokens || 0)-(a.selected?.tokens || 0);
      return (b.latest_activity || b.started_at || 0)-(a.latest_activity || a.started_at || 0);
    });
  }
  function sessionTable() {
    const box=root?.querySelector(".dash-sessions-table");if(!box || !current)return;
    const rows=filteredSessions(current.sessions);
    box.innerHTML=rows.length ? I18n.html`<table class="list dash-session-list"><thead><tr><th>会话与成果</th><th>模式 / 状态</th><th>最近活动（北京）</th><th>最近成果（北京）</th><th>本时段 Token</th><th>本时段费用估算</th><th>输入缓存命中率</th><th>最近一小时费用</th></tr></thead><tbody>${rows.map(s=>`<tr><td>${jump(s,s.title)}<small>${esc(outcome(s.outcomes))}</small>${s.data_note ? `<small>${esc(s.data_note)}</small>` : ""}</td><td>${esc(MODE_CN[s.mode] || s.mode)}<small>${esc(s.paused ? I18n.t("已暂停") : STATUS_CN[s.status] || s.status)}</small></td><td>${clock(s.latest_activity)}</td><td>${clock(s.last_result)}${!s.history_complete && !s.last_result ? I18n.html('<small>旧记录未提供</small>') : ""}</td><td>${compact(s.selected?.tokens || 0)}</td><td>${money(s.selected?.cost)}</td><td>${percent(s.selected?.cache?.hit_rate)}</td><td>${money(s.recent_hour?.cost)}</td></tr>`).join("")}</tbody></table>` : empty(I18n.t("没有符合当前筛选的会话"));
  }
  function cacheTable() {
    const box=root?.querySelector(".dash-cache-table");if(!box || !current)return;
    const byId=new Map(current.sessions.map(s=>[s.session_id,s]));
    const rows=(current.cache_groups[state.dimension] || []).map(r=>({...r,label:state.dimension === "session_id" ? byId.get(r.name)?.title || r.name : state.dimension === "actor" && typeof actorName === "function" ? actorName(r.name) : state.dimension === "model" ? modelDisplayName(r.name) : r.name})).filter(r=>r.label.toLowerCase().includes(state.cacheQuery.toLowerCase()));
    box.innerHTML=rows.length ? I18n.html`<table class="list"><thead><tr><th>${({model:I18n.t("模型"),actor:I18n.t("角色"),session_id:I18n.t("会话")})[state.dimension]}</th><th>输入缓存命中率</th><th>命中 / 未命中输入</th><th>输出 Token</th><th>用量完整性</th><th>费用估算</th></tr></thead><tbody>${rows.sort((a,b)=>b.tokens-a.tokens).map(r=>I18n.html`<tr><td>${state.dimension === "session_id" ? jump({session_id:r.name},r.label) : esc(r.label)}<small>${num(r.calls)} 次调用 · ${compact(r.tokens)} Token</small></td><td><span class="dash-rate">${percent(r.cache?.hit_rate)}</span></td><td>${compact(r.cache?.cached_input_tokens || 0)} / ${compact(r.cache?.uncached_input_tokens || 0)}<small>未测量的输入不计入这两项</small></td><td>${compact(r.cache?.output_tokens || 0)}</td><td>${num(r.cache?.measured_calls || 0)} 次已测量<small>${num(r.cache?.unmeasured_calls || 0)} 次未提供缓存计量</small></td><td>${money(r.cost)}<small>${coverage(r)}</small></td></tr>`).join("")}</tbody></table>` : empty(I18n.t("当前范围没有符合条件的用量分组"));
  }
  function usageBreakdown(summary) {
    const c=summary.cache || {}, input=summary.input_tokens ?? c.total_input_tokens ?? 0, output=summary.output_tokens ?? c.output_tokens ?? 0;
    const cached=c.cached_input_tokens || 0, uncached=c.uncached_input_tokens || 0;
    const unknown=Math.max(0,input-cached-uncached), total=input+output;
    const parts=[{label:I18n.t("缓存命中输入"),value:cached,tone:"cache"},{label:I18n.t("未命中输入"),value:uncached,tone:"input"},{label:I18n.t("缓存未测量输入"),value:unknown,tone:"unknown"},{label:I18n.t("输出"),value:output,tone:"output"}];
    root.querySelector(".dash-composition").innerHTML=total ? I18n.html`<div class="dash-token-total"><strong>${num(total)}</strong><span>已报告 Token</span></div><div class="dash-stack" role="img" aria-label="${esc(parts.map(p=>`${p.label} ${num(p.value)} Token`).join('，'))}">${parts.filter(p=>p.value>0).map(p=>`<span data-tone="${p.tone}" style="width:${p.value/total*100}%" title="${p.label}：${num(p.value)}"></span>`).join("")}</div><div class="dash-composition-key">${parts.map(p=>`<div><span><i data-tone="${p.tone}"></i>${p.label}</span><strong>${num(p.value)}</strong></div>`).join("")}</div>` : empty(I18n.t("尚无输入 / 输出分项记录；旧记录可在 Token 总数中查看"));
    const rows=[...(current.cache_groups.model || [])].sort((a,b)=>b.tokens-a.tokens), peak=Math.max(1,...rows.map(r=>r.tokens || 0));
    root.querySelector(".dash-models").innerHTML=rows.length ? rows.slice(0,5).map(r=>I18n.html`<article class="dash-model"><div><span title="${esc(r.name)}">${esc(modelDisplayName(r.name))}</span><strong>${compact(r.tokens)}</strong></div><div class="dash-model-track"><span style="width:${Math.max(0,r.tokens || 0)/peak*100}%"></span></div><small>${num(r.calls)} 次调用 · ${summary.tokens ? (r.tokens/summary.tokens*100).toFixed(1) : '0.0'}% 的已记录用量</small></article>`).join("") : empty(I18n.t("本时段暂无模型用量"));
  }
  function paint() {
    const d=current;if(!d || !root)return;
    root.querySelectorAll("[data-dash-period]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.dashPeriod === state.period)));
    root.querySelectorAll("[data-dash-metric]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.dashMetric === state.metric)));
    root.querySelectorAll("[data-dash-dimension]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.dashDimension === state.dimension)));
    root.querySelector(".dash-updated").textContent=I18n.t`北京时间 · ${clock(d.generated_at)} 更新`;
    const note=root.querySelector(".dash-notice");note.hidden=!d.legacy && !d.unallocated_tokens;
    if(d.unallocated_tokens) note.textContent=I18n.t`累计记录 ${compact(d.total_tokens)} Token，其中 ${compact(d.unallocated_tokens)} 缺少逐笔时间，未计入时段趋势。`;
    if(d.legacy) note.textContent=I18n.t("当前服务尚未提供新版聚合指标，暂展示已有统计；新指标在服务下次启动后可用。");
    const o=d.sessions.reduce((a,s)=>{a.flags+=s.outcomes?.flags_accepted || 0;a.vulns+=s.outcomes?.confirmed_vulns || 0;return a;},{flags:0,vulns:0});
    const card=(title,value,detail,tone)=>`<article class="dash-stat" data-tone="${tone}"><span>${title}</span><strong>${value}</strong><small>${detail}</small></article>`;
    const summary=d.summary, c=summary.cache || {};
    root.querySelector(".dash-overview").innerHTML=
      card(`${periods[state.period]} · Token`,compact(summary.tokens),I18n.t`输入 ${compact(summary.input_tokens ?? c.total_input_tokens ?? 0)} · 输出 ${compact(summary.output_tokens ?? c.output_tokens ?? 0)}`,"input")+
      card(I18n.t("模型调用"),num(summary.calls),summary.sdk_attempts == null ? I18n.t("SDK 请求数未提供") : I18n.t`${num(summary.sdk_attempts)} 次 SDK 请求（含重试）`,"output")+
      card(I18n.t("费用估算"),money(summary.cost),coverage(summary),"cost")+
      card(I18n.t("输入缓存命中率"),percent(c.hit_rate),I18n.t`${num(c.measured_calls || 0)} 次已测量 · ${num(c.unmeasured_calls || 0)} 次未提供`,"cache");
    root.querySelector(".dash-health").innerHTML=I18n.html`<span><i aria-hidden="true"></i>${num(d.sessions.filter(s=>s.status === "running" && !s.paused).length)} 个会话运行中</span><span>累计成果 <b>${num(o.flags)}</b> Flag / <b>${num(o.vulns)}</b> 漏洞</span><span class="${summary.usage_incomplete_calls ? "dash-incomplete" : ""}">${summary.usage_reported_calls == null ? I18n.t("历史记录未提供计量完整性") : I18n.t`计量完整 ${num(summary.usage_reported_calls)} / ${num(summary.calls)} 次调用 · ${num(summary.unreported_attempts || 0)} 次请求用量未完整返回`}</span>`;
    usageBreakdown(summary);
    const active=d.sessions.filter(s=>["running","created"].includes(s.status));
    root.querySelector(".dash-active-count").textContent=I18n.t`${active.filter(s=>s.status === "running").length} 个运行中 · ${active.reduce((n,s)=>n+(s.tasks.running || 0),0)} 个任务在途`;
    root.querySelector(".dash-active").innerHTML=active.length ? active.map(s=>I18n.html`<article class="dash-live-session"><header>${jump(s,s.title)}<span class="dash-live-status">${esc(s.paused ? I18n.t("已暂停") : phases[s.stage || s.phase] || STATUS_CN[s.status] || s.status)}</span></header><p>${esc(s.latest_progress)}</p><div class="dash-live-facts"><span>执行 ${num(s.tasks.running)} · 待发 ${num(s.tasks.pending)}</span><span title="${esc(s.capacity.source || I18n.t("容量未记录"))}">${s.capacity.limit ? I18n.t`任务占用 ${s.capacity.occupied_tasks}/${s.capacity.limit} · 空闲 ${s.capacity.free}` : I18n.t("容量未提供")}</span><span>最近活动 ${clock(s.latest_activity)}</span></div>${s.waiting_reason ? `<p class="dash-wait">${esc(s.waiting_reason)}</p>` : ""}<footer>${esc(outcome(s.outcomes))}${s.last_result ? I18n.t` · 最近成果 ${clock(s.last_result)}` : I18n.t(" · 成果时间未记录")}</footer></article>`).join("") : empty(I18n.t("当前没有运行或排队中的会话"));
    root.querySelector(".dash-attention").innerHTML=d.alerts.length ? d.alerts.map(a=>`<article class="dash-alert"><strong>${esc(a.text)}</strong><p>${esc(a.evidence)}</p>${jump(a,a.session_title)}<small>${clock(a.ts)}</small></article>`).join("") : empty(I18n.t("当前记录未触发提醒。"));
    const trend=root.querySelector(".dash-trend"), chart=trendChart(d.trend,state.metric);
    if(trend._content!==chart) {
      const expanded=trend.querySelector("details")?.open, scrollLeft=trend.querySelector(".dash-chart-viewport")?.scrollLeft || 0;
      const table=trend.querySelector(".dash-table-scroll"), top=table?.scrollTop || 0, left=table?.scrollLeft || 0;
      const tooltipIndex=trend.querySelector(".dash-chart-tooltip:not([hidden])")?.dataset.bucket;
      trend.innerHTML=chart;trend._content=chart;
      if(trend.querySelector(".dash-chart-viewport")) trend.querySelector(".dash-chart-viewport").scrollLeft=scrollLeft;
      if(expanded && trend.querySelector("details")) trend.querySelector("details").open=true;
      const nextTable=trend.querySelector(".dash-table-scroll");
      if(nextTable){nextTable.scrollTop=top;nextTable.scrollLeft=left;}
      if(tooltipIndex != null) {
        const target=trend.querySelector(`[data-dash-bucket="${tooltipIndex}"]`);
        if(target)showChartTip({target});
      }
    }
    root.querySelector(".dash-cache-windows").innerHTML=[5,30,60].map(m=>{const c=d.cache_windows[String(m)]?.cache;return I18n.html`<article><span>最近 ${m} 分钟</span><strong>${percent(c?.hit_rate)}</strong><small>${num(c?.measured_calls || 0)} 次已测量 / ${num(c?.unmeasured_calls || 0)} 次未提供</small></article>`;}).join("");
    const findings=d.findings;
    root.querySelector(".dash-recent-findings").innerHTML=findings.length ? findings.map(f=>`<article class="dash-finding"><header><span>${esc(({asset:I18n.t("入口 / 资产"),note:I18n.t("笔记"),flag:I18n.t("Flag 发现")})[f.type] || f.type || I18n.t("发现"))}</span><time>${clock(f.ts)}</time></header><p>${esc(f.text)}</p>${jump(f,f.session_title)}</article>`).join("") : empty(I18n.t("各会话尚无发现记录"));
    const exps=d.experience?.items || [];
    root.querySelector(".dash-experience-count").textContent=I18n.t`累计 ${num(d.experience?.total || 0)} 条`;
    root.querySelector(".dash-experience").innerHTML=exps.length ? exps.map(e=>`<article class="dash-finding"><p>${esc(e.insight || "")}</p><small>${esc((e.tags || []).join(" · "))}</small></article>`).join("") : empty(I18n.t("尚无跨会话经验"));
    sessionTable();cacheTable();
  }
  function render(box,data) {
    root=box;current=normalize(data);
    if(!box.querySelector(".dash-console")) scaffold(box);
    paint();
  }
  function error(box,message) {
    const note=box.querySelector(".dash-notice");
    if(note){note.hidden=false;note.textContent=I18n.t`刷新失败：${message}。保留上次数据，时间以最后更新为准。`;}
    else box.innerHTML=empty(I18n.t`仪表盘读取失败：${message}`);
  }
  return {render,error,get period(){return state.period;},clock,money,percent,normalize,filteredSessions,trendChart,tooltipContent};
})();
