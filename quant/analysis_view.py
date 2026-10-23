"""9989 看板的独立股票买点分析页（HTML 渲染和严格的转义边界）。"""

from __future__ import annotations

import datetime as dt
import html
from urllib.parse import quote as url_quote, urlsplit

from quant.analysis import assess_entry


def escape(value):
    return html.escape(str(value if value is not None else "—"), quote=True)


def amount(value):
    return f"{float(value) / 1e8:+.2f} 亿" if isinstance(value, (int, float)) else "无可用数据"


def number(value, suffix="", digits=2):
    return (f"{float(value):.{digits}f}{suffix}"
            if isinstance(value, (int, float)) else "—")


def news_link(item):
    url = str(item.get("url") or "")
    try:
        parsed = urlsplit(url)
        parsed.port  # 非法端口会抛 ValueError，不能输出可点击链接。
        valid = bool(parsed.scheme in ("http", "https") and parsed.netloc
                     and not parsed.username and not parsed.password
                     and parsed.hostname and not any(c.isspace() for c in url))
    except ValueError:
        return escape(item.get("title") or "无标题")
    if not valid:
        return escape(item.get("title") or "无标题")
    return (f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
            f'{escape(item.get("title") or "无标题")}</a>')


_QUERY_SUGGEST_SCRIPT = """<script>
(function(){
  var input=document.getElementById("analysis-query");
  var list=document.getElementById("analysis-query-list");
  if(!input||!list)return;
  var timer=null,items=[];
  function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
    return "&#"+c.charCodeAt(0)+";";});}
  function hide(){list.style.display="none";}
  function render(rows){
    items=rows||[];
    if(!items.length){hide();return;}
    list.innerHTML=items.map(function(r,i){
      return "<div data-i="+i+" style='padding:7px 10px;cursor:pointer;display:flex;gap:10px;align-items:center'>"+
        "<b>"+esc(r.code)+"</b><span>"+esc(r.name)+"</span>"+
        "<span style='opacity:.55'>"+esc(r.industry||"")+"</span></div>";}).join("");
    list.style.display="block";}
  input.addEventListener("input",function(){
    clearTimeout(timer);
    var q=input.value.trim();
    if(!q){hide();return;}
    timer=setTimeout(function(){
      fetch("/stock/search?q="+encodeURIComponent(q),{cache:"no-store"})
        .then(function(r){return r.json();})
        .then(function(d){render(d.ok?d.items:[]);})
        .catch(function(){hide();});},200);});
  list.addEventListener("mousedown",function(e){
    var row=e.target.closest("[data-i]");
    if(!row)return;
    var it=items[+row.getAttribute("data-i")];
    if(!it)return;
    input.value=it.code;
    hide();e.preventDefault();
    if(input.form)input.form.submit();});
  input.addEventListener("blur",function(){setTimeout(hide,180);});
  input.addEventListener("keydown",function(e){if(e.key==="Escape")hide();});
})();
</script>"""


def search_form(value=""):
    """统一搜索框:股票代码/名称走单股分析,板块名走板块优质股筛选。

    「⏱ 当前时段实时决策」与「分析买点」同行(type=button 不触发提交)。
    输入即联想下拉(/stock/search 走 cn_stock_spot 最新目录,与持仓编辑
    输入同一端点;点选回填代码并直接提交分析。板块名联想不到时原样提交,
    仍走板块筛选,互不影响。
    """
    return ("<form class='analysis-search' action='/analyze' method='get' "
            "role='search' style='position:relative'>"
            "<label for='analysis-query'>股票 / 板块</label>"
            f'<input id="analysis-query" name="query" value="{escape(value)}" '
            "placeholder='股票代码、名称或板块名，如：002815、大金融、自动化设备' "
            "maxlength='24' required autocomplete='off'>"
            "<div id='analysis-query-list' style='display:none;position:absolute;"
            "z-index:40;left:0;min-width:300px;max-height:260px;overflow:auto;"
            "background:#141b26;border:1px solid #3a4b63;border-radius:6px;"
            "box-shadow:0 6px 18px rgba(0,0,0,.45)'></div>"
            "<button type='submit'>分析买点</button>"
            "<button type='button' id='live-reco-btn' class='live-reco-inline'"
            " title='按当前时段实时给出可买候选与持仓卖出信号'>⏱ 当前时段实时决策</button>"
            "</form>" + _QUERY_SUGGEST_SCRIPT)


def intro(value="", details=""):
    return ("<div class='analysis-intro'><p class='analysis-kicker'>单股研判 / 条件单</p>"
            "<h1 class='analysis-title'>买点分析</h1>"
            "<p>输入一只 A 股，核对技术趋势、资金双口径、板块和新闻来源。"
            "买点是条件计划，不是实时报单。</p>" + search_form(value) +
            (f"<p class='analysis-hint' role='status'>{escape(details)}</p>" if details else "") +
            "</div>")


def live_reco_control(auto_sector=""):
    """实时决策面板容器 + 前端脚本(按钮由 search_form 提供,与「分析买点」同行)。

    实时决策:当前时段可买候选(核心条件通过率 + 建议买入区/止损价)与
    持仓卖出信号;板块页传入 auto_sector 时加载即自动筛选板块优质股。
    结果与个股分析/搜索结果互斥展示,一次只显示一个;只做展示,不自动买卖。
    """
    button = ("<div id='live-reco-panel' class='live-reco-panel' aria-live='polite'"
              f" data-sector='{escape(auto_sector)}'></div>")
    script = """<script>
(function(){
  const btn=document.getElementById('live-reco-btn');
  const panel=document.getElementById('live-reco-panel');
  if(!panel)return;
  const esc=s=>String(s==null?'—':s).replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const link=b=>'<a href="/analyze?query='+encodeURIComponent(b.code)+'">'
    +'<b>'+esc(b.name)+'</b> '+esc(b.code)+'</a>';
  const fmtFlow=v=>{if(v==null)return '—';
    const a=Math.abs(v);
    const t=a>=1e8?(a/1e8).toFixed(2)+'亿'
      :a>=1e4?Math.round(a/1e4).toLocaleString()+'万'
      :Math.round(a).toLocaleString()+'元';
    return (v>0?'+':v<0?'-':'')+t;};
  const flowHtml=v=>'<br><span class="'+(v>0?'up':(v<0?'down':'small'))
    +'">净额 '+fmtFlow(v)+'</span>';
  const annBadge=a=>!a?'':(a.status==='pass'
    ?'<br><span class=small>✓ 巨潮公告核验过('+esc(a.summary)+')</span>'
    :a.status==='warning'
    ?'<br><span class=small>⚠ '+(a.url?'<a href="'+esc(a.url)+'" target="_blank" rel="noopener noreferrer">公告原文</a> ':'')+esc(a.summary)+'</span>'
    :a.status==='pending'
    ?'<br><span class=small>⏳ 巨潮公告核验中(稍候再看)…</span>'
    :'<br><span class=small>'+esc(a.summary||'巨潮未核验')+'</span>');
  const takeResult=()=>document.getElementById('analysis-result');
  const bindBack=result=>{const back=panel.querySelector('#live-reco-back');
    if(back)back.addEventListener('click',()=>{panel.innerHTML='';
      if(result)result.style.display='';else location.href='/analyze';});};
  const head=(title)=>'<div class="live-reco-head"><button type="button" '
    +'id="live-reco-back">← 返回个股分析</button><h2>'+title+'</h2></div>';
  const foot=(extra)=>'<p class="analysis-hint">生成于 '+esc(extra.at)
    +(extra.note?' · '+esc(extra.note):'')
    +' · 点击股票名进入单股九项条件分析；规则化信号，不构成投资建议。</p>';

  if(btn){
    btn.addEventListener('click',async()=>{
      const result=takeResult();
      if(result)result.style.display='none';
      btn.disabled=true;const old=btn.textContent;btn.textContent='采集中…';
      panel.innerHTML='<p class="analysis-hint">正在按当前时段实时确认：可买候选(核心条件+入场价)与持仓卖出信号…</p>';
      try{
        const r=await fetch('/recommend/live',{cache:'no-store'});
        const d=await r.json();
        if(!d.ok)throw new Error(d.error||'获取失败');
        if(d.buys_loading){
          panel.innerHTML='<p class="analysis-hint">选股因子正在后台加载，请稍后再次点击。</p>';
          return;
        }
        const buys=(d.buys||[]).map(b=>{
          const plan=(b.plan_low&&b.plan_high)?esc(b.plan_low)+'～'+esc(b.plan_high):'不可计算';
          const stop=b.stop?esc(b.stop):'—';
          const tps=(b.tp1||b.tp2)?((b.tp1?esc(b.tp1):'—')+' / '+(b.tp2?esc(b.tp2):'—')):'—';
          const flow=v=>v==null?'':'<span class="'+(v>0?'up':(v<0?'down':'small'))
            +'">净额 '+(v>0?'+':'')+(Math.abs(v)>=1e8
            ?(Math.abs(v)/1e8).toFixed(2)+'亿'
            :Math.round(Math.abs(v)/1e4).toLocaleString()+'万')
            +'</span>';
          return '<tr><td>'+link(b)+'</td>'
            +'<td class=small>'+esc(b.grade)+'/'+esc(b.horizon)+'<br>'+esc(b.score10)+'分</td>'
            +'<td class=n>'+esc(b.price)+'<br>'+esc(b.pct)+'%'
            +(b.flow_net==null?'':'<br>'+flow(b.flow_net))+'</td>'
            +'<td class=small>'+plan+'<br>'+esc(b.setup)+'</td>'
            +'<td class=small>止损 '+stop+'<br>止盈 '+tps+'</td>'
            +'<td class=n>'+esc(b.core_pass)+'/'+esc(b.core_total)+'<br>'
            +'<span class=small>'+esc(b.verdict_tag)+'</span></td>'
            +'<td class=small>'+esc(b.failed)+'</td>'
            +'<td class=small>'+esc(b.evidence)+'；'+esc(b.confirm)
            +annBadge(b.announce)+'</td></tr>';
        }).join('');
        const sells=(d.sells||[]).map(s=>'<tr><td>'+link(s)+'</td><td class=n>'
          +esc(s.now)+'</td><td>'+esc(s.pct)+'%</td><td>'
          +(s.pl_pct?esc(s.pl_pct)+'%':'—')+'</td><td><b>'+esc(s.action)+'</b></td>'
          +'<td class=small>'+esc(s.why)+'</td><td class=small>'+esc(s.levels)+'</td></tr>').join('');
        let html=head('⏱ 当前时段实时决策 · '+esc(d.slot_label));
        html+='<h3>当前时段可买候选（主板 · 核心条件 + 实时确认）</h3>';
        html+=buys?'<table><tr><th>股票</th><th>级/线·分</th><th>现价/涨跌</th>'
          +'<th>建议买入区</th><th>止损/止盈</th><th>核心</th><th>未过项</th>'
          +'<th>共振与时段确认依据</th></tr>'+buys+'</table>'
          :'<p class="analysis-hint">'+esc(d.buy_note||'当前无同时满足共振与时段确认的标的（不硬凑）。')+'</p>';
        if(buys&&d.buy_note)html+='<p class="analysis-hint">'+esc(d.buy_note)+'</p>';
        html+='<h3>持仓卖出信号</h3>';
        html+=sells?'<table><tr><th>股票</th><th>现价</th><th>涨跌</th>'
          +'<th>浮盈亏</th><th>建议动作</th><th>依据</th><th>关键位</th></tr>'
          +sells+'</table>'
          :'<p class="analysis-hint">'+esc(d.sell_note||'当前持仓无卖出信号。')+'</p>';
        const kd=(d.buys&&d.buys[0]&&d.buys[0].kline_date)||'';
        const today=(d.generated_at||'').slice(0,10);
        const knote=(kd&&kd.slice(0,10)!==today)?'技术指标基于本地K线至 '+esc(kd)
          +'（收盘链自动补齐后恢复完整口径）':'';
        html+=foot({at:d.generated_at,note:(d.slot_note||'')+(knote?' · '+knote:'')});
        panel.innerHTML=html;
        bindBack(result);
      }catch(e){
        panel.innerHTML='<p class="analysis-hint">获取失败：'+esc(e.message||e)+'，请稍后重试。</p>';
        if(result)result.style.display='';
      }finally{btn.disabled=false;btn.textContent=old;}
    });
  }

  const runSector=async(q)=>{
      const result=takeResult();
      if(result)result.style.display='none';
      panel.innerHTML='<p class="analysis-hint">正在按当日情绪与周期筛选板块优质股…</p>';
      try{
        const r=await fetch('/sector/pick?sector='+encodeURIComponent(q),{cache:'no-store'});
        const d=await r.json();
        if(!d.ok)throw new Error(d.error||'筛选失败');
        const rows=(d.rows||[]).map(b=>{
          const plan=(b.plan_low&&b.plan_high)?esc(b.plan_low)+'～'+esc(b.plan_high):'不可计算';
          const stop=b.stop?esc(b.stop):'—';
          const tps=(b.tp1||b.tp2)?((b.tp1?esc(b.tp1):'—')+' / '+(b.tp2?esc(b.tp2):'—')):'—';
          return '<tr><td>'+link(b)+'</td>'
            +'<td class=small><b>'+esc(b.cycle_label)+'</b><br>'+esc(b.cycle_why)+'</td>'
            +'<td class=n>'+esc(b.price)+(b.realtime?'':'<br><span class=small>喂表价</span>')
            +'<br>'+esc(b.pct)+'%</td>'
            +'<td class=small>'+plan+'<br>'+esc(b.setup)+'</td>'
            +'<td class=small>止损 '+stop+'<br>止盈 '+tps+'</td>'
            +'<td class=n>'+esc(b.quality)+'/6<br>'
            +'<span class=small>'+(b.trend_ok&&b.position_ok?'趋势+位置过'
              :(b.trend_ok?'仅趋势过':'仅位置过'))+'</span></td>'
            +'<td class=small>'+esc(b.amount_yi)+'亿</td>'
            +'<td class=small>'+esc(b.evidence)+annBadge(b.announce)+'</td></tr>';
        }).join('');
        const s=d.sentiment||{}, c=d.cycle, sec=d.sector||{};
        let html=head('板块优质股 · '+esc(sec.names||''));
        html+='<p class="analysis-hint">市场情绪 '+esc(s.score)+'/100（'+esc(s.label)
          +'）：上涨占比 '+esc(s.up_ratio)+'%、涨停近似 '+esc(s.limit_up)
          +' 家、大涨(≥5%) '+esc(s.hot_ratio)+'%，共 '+esc(s.total)+' 只。</p>';
        html+='<p class="analysis-hint">'+(c?'大盘周期 '+esc(c.label)+'（近5日 '+esc(c.chg5)
          +'%，连'+(c.streak>=0?'涨':'跌')+Math.abs(c.streak)+'日，MA5 '+esc(c.ma5)
          +' / MA10 '+esc(c.ma10)+'，截至 '+esc(c.as_of)+'）':'大盘周期数据不可用，仅按当日情绪判断')
          +' · 板块当日 '+esc(sec.avg_pct)+'%（上涨占比 '+esc(Math.round((sec.up_ratio||0)*100))
          +'%，热度 '+esc(sec.rank)+'/'+esc(sec.total)+'）</p>';
        html+='<h3>板块优质股（情绪门控 + 周期判断 + 技术评估）</h3>';
        html+=rows?'<table><tr><th>股票</th><th>周期</th><th>现价/涨跌</th>'
          +'<th>建议买入区</th><th>止损/止盈</th><th>优质分</th><th>额</th>'
          +'<th>趋势/位置/周期/情绪依据</th></tr>'+rows+'</table>'
          :'<p class="analysis-hint">该板块无通过筛选的优质股（情绪门控/周期剔除后为空，不硬凑）。</p>';
        if(d.gate_note)html+='<p class="analysis-hint">'+esc(d.gate_note)+'</p>';
        if(d.meta)html+='<p class="analysis-hint">技术评估跳过 '+esc(d.meta.skipped)
          +' 只（历史K不足）；周期剔除高位过热 '+esc(d.meta.dropped_hot)
          +' 只、下行退潮 '+esc(d.meta.dropped_retreat)
          +' 只；优质分不足2分过滤 '+esc(d.meta.below_min)+' 只。</p>';
        const m=d.meta||{};
        const kd=(d.rows&&d.rows[0]&&d.rows[0].kline_date)||'';
        const today=(d.generated_at||'').slice(0,10);
        let note=(d.spot_note||'');
        if(kd&&kd.slice(0,10)!==today)note+=(note?' · ':'')+'技术指标基于本地K线至 '+esc(kd);
        html+=foot({at:d.generated_at,note:note});
        panel.innerHTML=html;
        bindBack(result);
      }catch(e){
        panel.innerHTML='<p class="analysis-hint">筛选失败：'+esc(e.message||e)+'，请稍后重试。</p>';
      }
    };
    const auto=panel.getAttribute('data-sector');
    if(auto)runSector(auto);
})();
</script>"""
    return button + script


def sector_page(query, names, details=""):
    """板块识别落地页:说明命中的板块,加载后自动筛选优质股。"""
    intro_html = ("<div class='analysis-intro'><p class='analysis-kicker'>板块优质股 / 情绪周期</p>"
                  "<h1 class='analysis-title'>板块筛选</h1>"
                  f"<p>「{escape(query)}」识别为板块：{escape('、'.join(names))}。"
                  "按当日情绪门控与大盘/个股周期筛选优质股；输入股票代码或名称则进入单股九项条件分析。</p>"
                  + search_form(query) +
                  (f"<p class='analysis-hint' role='status'>{escape(details)}</p>" if details else "") +
                  "</div>")
    return intro_html + live_reco_control(query)


def strategies_shell(details=""):
    """战法选股页:三路战法按可信度排序 + 分战法 tab + 前向可行性统计。

    可信度 = 单战法基础分 + 多战法叠加 + 情绪/大盘/个股周期环境修正(0-100);
    tab 细分:M3 多共振 / 龙头战法 / 板块优质 各自独立列表(按命中战法过滤);
    可行性=每日收盘候选的前向追踪样本(quant_strategy_trades,T+N 自动回填),
    不做无历史数据支撑的回测,样本不足时如实标注。连板梯队为提示区
    (高标只看不追、非主板标注交易权限外)。加载即自动拉取
    /strategies/data;只做展示,不构成投资建议,不自动买卖。
    """
    intro_html = ("<div class='analysis-intro'><p class='analysis-kicker'>战法选股 / 可信度排序</p>"
                  "<h1 class='analysis-title'>战法选股</h1>"
                  "<p>聚合三路战法信号——M3 多共振、龙头战法（连板梯队）、当日热度板块优质股——"
                  "按市场情绪与大盘/个股周期做环境修正后计算可信度（0-100），从高到低排序；"
                  "下方按战法分 tab 细分，各战法的可行性用每日候选的前向追踪样本统计"
                  "（T+1 胜率/平均涨幅），样本从记录日起累积、不足时如实标注。"
                  "连板股按 T+1 竞价思路参考。</p>"
                  + search_form() +
                  (f"<p class='analysis-hint' role='status'>{escape(details)}</p>" if details else "") +
                  "</div>")
    panel = ("<div id='strategies-panel' class='live-reco-panel' aria-live='polite'>"
             "<p class='analysis-hint'>正在聚合三路战法信号（M3 共振 / 龙头梯队 / 板块优质）"
             "并计算可信度…</p></div>")
    script = """<script>
(function(){
  const panel=document.getElementById('strategies-panel');
  if(!panel)return;
  const esc=s=>String(s==null?'—':s).replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const link=b=>'<a href="/analyze?query='+encodeURIComponent(b.code)+'">'
    +'<b>'+esc(b.name)+'</b> '+esc(b.code)+'</a>';
  const fmtFlow=v=>{if(v==null)return '—';
    const a=Math.abs(v);
    const t=a>=1e8?(a/1e8).toFixed(2)+'亿'
      :a>=1e4?Math.round(a/1e4).toLocaleString()+'万'
      :Math.round(a).toLocaleString()+'元';
    return (v>0?'+':v<0?'-':'')+t;};
  const flowHtml=v=>'<br><span class="'+(v>0?'up':(v<0?'down':'small'))
    +'">净额 '+fmtFlow(v)+'</span>';
  const annCell=a=>!a?'<span class=small>未核验</span>':(a.status==='pass'
    ?'<span class=small>✓ '+esc(a.summary)+'</span>'
    :a.status==='warning'
    ?'<span class=small>⚠ '+(a.url?'<a href="'+esc(a.url)+'" target="_blank" rel="noopener noreferrer">原文</a> ':'')+esc(a.summary)+'</span>'
    :a.status==='pending'
    ?'<span class=small>⏳ 核验中…</span>'
    :'<span class=small>'+esc(a.summary||'未核验')+'</span>');
  let tries=0;
  const run=async()=>{
    try{
      const r=await fetch('/strategies/data',{cache:'no-store'});
      const d=await r.json();
      if(!d.ok)throw new Error(d.error||'获取失败');
      if(d.loading){panel.innerHTML='<p class="analysis-hint">三路信号正在后台加载'
        +'（选股因子/涨停池/板块成分首次请求需预热），请稍候手动刷新。</p>';return;}
      const s=d.sentiment||{},c=d.cycle;
      let html='<h2>战法选股 · 按可信度排序</h2>';
      html+='<p class="analysis-hint">市场情绪 '+esc(s.score)+'/100（'+esc(s.label)
        +'）· '+(c?'大盘周期 '+esc(c.label)+'（近5日 '+esc(c.chg5)
        +'%）':'大盘周期不可用')
        +'；可信度环境修正：'+esc(d.env_note||'无')+'。</p>';
      const rowHtml=b=>{
        const tagColor=b.confidence>=75?'#f0c24b':b.confidence>=50?'#5cb3ff':'#8593ab';
        const plan=(b.plan_low&&b.plan_high)?esc(b.plan_low)+'～'+esc(b.plan_high)
          :(String(b.price_from).indexOf('涨停')>=0?'T+1 竞价确认':'—');
        const stop=b.stop?esc(b.stop):'—';
        const tps=(b.tp1||b.tp2)?((b.tp1?esc(b.tp1):'—')+' / '+(b.tp2?esc(b.tp2):'—')):'—';
        return '<tr><td>'+link(b)+'</td>'
          +'<td class=n><b style="color:'+tagColor+';font-size:16px">'+esc(b.confidence)
          +'</b><br><span class=small style="color:'+tagColor+'">'+esc(b.tag)+'</span></td>'
          +'<td class=small>'+esc(b.methods)+(b.multi?'<br><b>多战法共振</b>':'')+'</td>'
          +'<td class=small>'+esc(b.cycle_label)+'</td>'
          +'<td class=n>'+esc(b.price)+'<br><span class=small>'+esc(b.price_from)
          +'</span><br>'+esc(b.pct)+'%'+flowHtml(b.flow_net)+'</td>'
          +'<td class=small>'+plan+'<br>'+esc(b.setup)+'</td>'
          +'<td class=small>止损 '+stop+'<br>止盈 '+tps+'</td>'
          +'<td class=small>'+esc(b.evidence)+'</td>'
          +'<td class=small>'+annCell(b.announce)+'</td></tr>';
      };
      const TABLE_HEAD='<tr><th>股票</th><th>可信度</th><th>命中战法</th><th>个股周期</th>'
        +'<th>现价·涨跌/资金净额</th><th>建议买入区</th><th>止损/止盈</th><th>战法依据</th>'
        +'<th>公告核验(巨潮)</th></tr>';
      const all=d.rows||[];
      const TABS=[['','全部（混合榜）'],['M3共振','M3 多共振'],['龙头战法','龙头战法'],['板块优质','板块优质']];
      html+="<div class='seg' role='tablist' aria-label='战法页签'>"+TABS.map((t,i)=>{
        const n=t[0]?all.filter(b=>String(b.methods||'').indexOf(t[0])>=0).length:all.length;
        return "<button type=button data-stab='"+i+"' class='stab"+(i?'':' active')+"'"
          +">"+esc(t[1])+" <span class='stab-n'>"+n+'</span></button>';
      }).join('')+'</div>';
      html+=TABS.map((t,i)=>{
        const sub=t[0]?all.filter(b=>String(b.methods||'').indexOf(t[0])>=0):all;
        const rs=sub.map(rowHtml).join('');
        return "<div id='stab-"+i+"' style='display:"+(i?'none':'block')+"'>"
          +(rs?'<table>'+TABLE_HEAD+rs+'</table>'
             :'<p class="analysis-hint">该战法当前无命中标的（情绪/周期环境剔除后为空，不硬凑）。</p>')
          +'</div>';
      }).join('');
      const st=d.method_stats||{};
      const grade=s=>(!s||!s.n_t1)?'样本积累中'
        :(s.n_t1<10?'样本<10，仅供参考'
          :(s.win1>=55?'历史样本较强':s.win1>=45?'历史样本中性':'历史样本偏弱'));
      const stRow=(k,label)=>{const s=st[k];if(!s)return '';
        return '<tr><td><b>'+esc(label)+'</b></td><td class=n>'+s.n+'</td>'
          +'<td class=n>'+s.n_t1+'</td>'
          +'<td class=n>'+(s.win1==null?'—':s.win1+'%')+'</td>'
          +'<td class=n>'+(s.win5==null?'—':s.win5+'%')+'</td>'
          +'<td class=n>'+(s.avg_t1==null?'—':((s.avg_t1>0?'+':'')+s.avg_t1+'%'))+'</td>'
          +'<td class=n>'+(s.conf==null?'—':s.conf)+'</td>'
          +'<td class=small>'+grade(s)+'</td></tr>';};
      html+='<h2>各战法可行性（前向追踪）</h2>'
        +'<table><tr><th>战法</th><th>样本</th><th>已回填T+1</th><th>T+1胜率</th>'
        +'<th>T+5胜率</th><th>平均T+1</th><th>均可信度</th><th>样本判定</th></tr>'
        +stRow('m3','M3 多共振')+stRow('leader','龙头战法')+stRow('sector','板块优质')
        +stRow('m3_push','M3+时段确认推送（参考口径）')
        +'</table>'
        +'<p class="analysis-hint">统计=每日收盘各战法候选的前向样本'
        +(st.since?('（'+esc(st.since)+' 起）'):'（今日开始记录）')
        +'，T+1 胜率=次日收盘＞记录时点价；推送口径=模拟盘每日 1-3 只精选信号，'
        +'两种样本口径不同不混算。历史样本表现不代表未来，不构成投资建议。</p>';
      if(d.theme){
        html+='<h2>连板梯队 · 主线题材 '+esc(d.theme)
          +(d.height?'（最高 '+esc(d.height)+' 板，梯队 '+esc(d.count)+' 家）':'')+'</h3>';
        const lad=(d.ladder||[]).map(l=>'<tr'+(l.watch_only?' style="opacity:.55"':'')
          +'><td>'+(l.watch_only?'<b>'+esc(l.name)+'</b> '+esc(l.code):link(l))+'</td>'
          +'<td class=n>'+esc(l.boards)+'板</td><td class=small>'+esc(l.limit_time)+'</td>'
          +'<td class=n>'+esc(l.seal_yi)+'亿'+flowHtml(l.flow_net)
          +'</td><td class=small>'+esc(l.state)+'</td>'
          +'<td class=small>'+esc(l.note)+'</td>'
          +'<td class=small>'+annCell(l.announce)+'</td></tr>').join('');
        html+=lad?'<table><tr><th>股票</th><th>高度</th><th>首封时间</th><th>封单/净额</th>'
          +'<th>状态</th><th>纪律提示</th><th>公告核验(巨潮)</th></tr>'+lad+'</table>'
          :'<p class="analysis-hint">今日无 2 连板以上梯队（首板不构成龙头结构）。</p>';
      }
      if(d.hints&&d.hints.length)html+='<p class="analysis-hint">'+d.hints.map(esc).join('<br>')+'</p>';
      html+='<p class="analysis-hint">'+esc(d.paper_note||'')+'</p>';
      html+='<p class="analysis-hint">资金净额=THS 即时全单口径(流入−流出,'
        +'含散单;收盘后为当日终值),与东财主力(超大+大单)口径不同不混用;'
        +'接口异常/非交易日显示 —,不补造。</p>'
      html+='<p class="analysis-hint">生成于 '+esc(d.generated_at)
        +' · 点击股票名进入单股九项条件分析；连板股现价为涨停价（T+1 按竞价确认），'
        +'高标与非主板仅作提示不推荐；可信度为规则化评分，不构成投资建议。</p>';
      panel.innerHTML=html;
      panel.querySelectorAll('[data-stab]').forEach(btn=>btn.addEventListener('click',()=>{
        panel.querySelectorAll('[data-stab]').forEach(x=>x.classList.remove('active'));
        panel.querySelectorAll('[id^=stab-]').forEach(x=>x.style.display='none');
        const box=document.getElementById('stab-'+btn.getAttribute('data-stab'));
        if(box)box.style.display='block';
        btn.classList.add('active');
      }));
      // 首次核验中的股票,4 秒后自动重拉一次(每股缓存 1 小时,几秒内即热)
      if(d.announce_pending&&tries<3){tries++;setTimeout(run,4000);}
    }catch(e){
      panel.innerHTML='<p class="analysis-hint">获取失败：'+esc(e.message||e)
        +'，请稍后刷新重试。</p>';
    }
  };
  run();
})();
</script>"""
    return intro_html + panel + script


def announcement_section(announcements):
    """交易所公告原文核验区块(巨潮资讯网,沪深法定披露平台)。

    展示近 30 日公告标题(链接到原文 PDF)与风险分级:停牌/退市/立案等
    高风险项、减持/问询等提示项逐条标注;未收录/核验中如实说明,不补造。
    """
    data = announcements or {}
    status = data.get("status") or ""
    items = data.get("items") or []
    hits = {h.get("title"): h for h in data.get("hits") or []}
    parts = ["<h2>交易所公告原文 · 巨潮核验</h2>"]
    if status in ("pending", ""):   # 未核验/旧样本无该键,均提示稍候
        parts.append("<p class='analysis-hint'>巨潮公告正在核验(每股缓存 1 小时，"
                     "稍候刷新即可看到近 30 日公告与原文链接)。</p>")
        return "".join(parts)
    if status == "unknown":
        parts.append(f"<p class='analysis-hint'>{escape(data.get('summary'))}；"
                     "不能据此判断该公司无公告或无风险。</p>")
        return "".join(parts)
    if status == "blocked":
        parts.append("<p class='analysis-hint'><b>存在高风险公告，不进入任何买入候选："
                     f"{escape(data.get('summary'))}。</b>请在原文中核对事项与影响范围。</p>")
    elif status == "warning":
        parts.append(f"<p class='analysis-hint'>公告风险提示：{escape(data.get('summary'))}；"
                     "候选页已据此下调可信度，请打开原文核对事项性质。</p>")
    else:
        parts.append(f"<p class='analysis-hint'>近 30 日 {escape(data.get('n'))} 条公告，"
                     "标题未命中高风险关键词；关键词粗筛可能漏判，原文请自行核对。</p>")
    if items:
        parts.append("<ol class='analysis-news'>")
        for item in items:
            hit = hits.get(item.get("title"))
            mark = ""
            if hit:
                mark = (" <b style='color:#c0392b'>[高风险·已剔除候选]</b>"
                        if hit.get("level") == "blocked"
                        else " <b style='color:#b8860b'>[风险提示]</b>")
            parts.append('<li><span class="analysis-news-meta">'
                        f'{escape(item.get("date"))} · 巨潮资讯网</span>'
                        f'<div>{news_link(item)}{mark}</div></li>')
        parts.append("</ol>")
    else:
        parts.append("<p class='analysis-hint'>近 30 日无新披露公告（0 条）。</p>")
    return "".join(parts)


def empty_page(value="", details="", candidates=None):
    parts = [intro(value, details), live_reco_control(), "<div id='analysis-result'>"]
    if candidates:
        parts.append("<h2>匹配候选</h2><div class='analysis-candidates'>")
        for item in candidates:
            code = str(item["code"])
            parts.append(f'<a href="/analyze?query={url_quote(code)}">'
                         f'{escape(item["name"])} <span>{escape(code)}</span></a>')
        parts.append("</div>")
    else:
        parts.append("<p class='analysis-empty'>可从顶部输入股票名称或六位代码。分析不更改持仓，"
                     "也不自动下单；休市后仅提供下一交易时段的条件参考。</p>")
    parts.append("</div>")
    return "".join(parts)


def _spot_prev_close(code):
    """cn_stock_spot 最新一行的昨收与数据日((prev, date)),无则 (None, None)。"""
    from quant.db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pre_close_price,`date` FROM cn_stock_spot "
                        "WHERE code=%s ORDER BY `date` DESC LIMIT 1", (code,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row and row[0]:
        return float(row[0]), str(row[1])
    return None, None


def minute_chart_section(code, name=""):
    """当日分时走势卡(通达信独立源,60 秒缓存,失败降级不阻断分析)。

    分时数据无昨收字段,基准线取喂表 cn_stock_spot 的 pre_close_price;
    两者数据日不一致(喂表非当日)时不画基准线,只展示日内相对走势。
    """
    from quant.datasource import tdx as _tdx
    try:
        frame = _tdx.fetch_minute_cached(code)
    except Exception:
        frame = None
    if frame is None or frame.empty:
        return ("<h2>分时走势</h2><p class='analysis-hint'>"
                "当日分时暂不可用(通达信源);不影响以上分析。</p>")
    prices = [float(v) for v in frame["price"].tolist()]
    vols = [max(float(v), 0.0) for v in frame["vol"].tolist()]
    n = len(prices)
    if n < 2:
        return ("<h2>分时走势</h2><p class='analysis-hint'>"
                "分时采样不足(通达信源仅 %d 条);不影响以上分析。</p>" % n)
    # 日内均价:分时无成交额,以价×量累计近似(量加权 VWAP)。
    cp = cv = 0.0
    avg = []
    for p, v in zip(prices, vols):
        cp += p * v
        cv += v
        avg.append(cp / cv if cv else p)
    try:
        prev, spot_day = _spot_prev_close(code)
    except Exception:
        prev, spot_day = None, None
    today = dt.date.today().strftime("%Y-%m-%d")
    has_base = bool(prev and spot_day == today)
    lo = min(prices + ([prev] if has_base else []))
    hi = max(prices + ([prev] if has_base else []))
    span = (hi - lo) or 0.01

    W, H, HV, GAP = 660, 150, 38, 6
    def _x(i):
        return 2 + (W - 4) * i / max(n - 1, 1)
    def _y(v):
        return 3 + (H - 6) * (1 - (v - lo) / span)
    line = " ".join("%.1f,%.1f" % (_x(i), _y(p)) for i, p in enumerate(prices))
    aline = " ".join("%.1f,%.1f" % (_x(i), _y(a)) for i, a in enumerate(avg))
    vmax = max(vols) or 1.0
    bw = max((W - 4) / n - 0.2, 0.6)
    vol_top = H + GAP
    bars = "".join('<rect x="%.1f" y="%.1f" width="%.2f" height="%.1f" '
                   'style="fill:var(--accent);opacity:.3"/>'
                   % (_x(i) - bw / 2, vol_top + HV * (1 - v / vmax), bw,
                      HV * v / vmax)
                   for i, v in enumerate(vols) if v > 0)
    base_line = ""
    if has_base:
        by = _y(prev)
        ty = by - 4 if by > 14 else by + 14        # 贴顶时标注移到线下,防出画布
        base_line = ('<line x1="2" x2="%d" y1="%.1f" y2="%.1f" '
                     'style="stroke:var(--muted);stroke-dasharray:4 3;opacity:.8"/>'
                     '<text x="%d" y="%.1f" style="fill:var(--muted);font-size:11px" '
                     'text-anchor="end">昨收 %.2f</text>'
                     % (W - 2, by, by, W - 6, ty, prev))
    last, mean = prices[-1], avg[-1]
    now = dt.datetime.now()
    end_label = "15:00" if n >= 235 else now.strftime("%H:%M")
    svg = (f'<svg viewBox="0 0 {W} {vol_top + HV + 16}" width="100%" '
           'role="img" aria-label="当日分时走势" style="display:block">'
           f'{base_line}'
           f'<polyline points="{aline}" style="fill:none;stroke:var(--gold);'
           'stroke-width:1;opacity:.9"/>'
           f'<polyline points="{line}" style="fill:none;stroke:var(--accent);'
           'stroke-width:1.6"/>'
           f'{bars}'
           f'<text x="2" y="{vol_top + HV + 13}" '
           'style="fill:var(--muted);font-size:11px">09:30</text>'
           f'<text x="{W - 2}" y="{vol_top + HV + 13}" '
           'style="fill:var(--muted);font-size:11px" text-anchor="end">'
           f'{escape(end_label)}</text>'
           f'<text x="{W / 2:.0f}" y="{vol_top + HV + 13}" '
           'style="fill:var(--muted);font-size:11px" text-anchor="middle">'
           f'{n} 分钟 · 均价线金色</text></svg>')

    chg = (last / prev - 1.0) if has_base else None
    amp = (hi - lo) / prev * 100 if has_base else None
    cls = "up" if (chg or 0) > 0 else ("down" if (chg or 0) < 0 else "")
    chg_html = (f'<b class="{cls}">{last:.2f}</b> '
                f'<span class="{cls}">{chg * 100:+.2f}%</span>') if chg is not None \
        else f"<b>{last:.2f}</b>"
    trend = ("现价运行在日内均价上方,盘中承接偏强" if last > mean * 1.001 else
             ("现价运行在日内均价下方,盘中承接偏弱" if last < mean * 0.999
              else "现价贴着日内均价,方向未选"))
    return ("<h2>分时走势 · 当日</h2><div class='analysis-minute'>"
            f"<div class='minute-quote'>最新 {chg_html} · 均价 {mean:.2f} · "
            f"最高 {hi:.2f} / 最低 {lo:.2f}"
            + (f" · 振幅 {amp:.1f}%" if amp is not None else "")
            + "</div>" + svg +
            f"<p class='analysis-hint'>{trend}。数据来自通达信分时(独立口径,与东财分钟 K "
            "互为校验;收盘后 240 分钟完整,盘中为到当前时刻的增量)。"
            + ("基准昨取自喂表当日快照。" if has_base
               else "今日喂表昨收未就绪,未画基准线。")
            + "</p></div>")


def render_analysis(facts, holding=None, total_assets=None, now=None):
    now = now or dt.datetime.now()
    result = assess_entry(facts, now=now, holding=holding, total_assets=total_assets)
    quote = facts.get("quote") or {}
    tech = facts.get("tech") or {}
    sector = facts.get("sector") or {}
    ths_flow = facts.get("ths_flow") or {}
    em_flow = facts.get("em_flow") or {}
    p = result["plan"]
    status = "ready" if result["ready"] else "waiting"
    if "不可买入" in result["verdict"]:
        status = "closed"
    body = [intro(facts["code"]),
            '<form action="/analyze/refresh" method="post" class="analysis-refresh">'
            f'<input type="hidden" name="code" value="{escape(facts["code"])}">'
            '<button type="submit">重新采集并分析</button></form>',
            live_reco_control(),
            # 结果与实时决策面板互斥展示:点击「当前时段实时决策」时整块隐藏。
            "<div id='analysis-result'>",
            f'<section class="analysis-verdict {status}" aria-live="polite">'
            '<div><span class="analysis-eyebrow">今日入场判断</span>'
            f'<strong>{escape(result["verdict"])}</strong>'
            f'<p>{result["passed"]}/{result["total"]} 条条件通过 · '
            f'评估于 {escape(now.strftime("%Y-%m-%d %H:%M:%S"))}；'
            f'数据采集完成 {escape(facts.get("completed_at"))}。'
            '不执行任何买卖。</p></div>'
            f'<div class="analysis-last"><span>{escape(facts["name"])} {escape(facts["code"])}</span>'
            f'<b>{number(quote.get("last_price"))}</b>'
            f'<span>THS 快照采集 {escape(facts.get("quote_at"))} · 数据日期 '
            f'{escape(facts.get("quote_date") or "待核验")} · '
            f'东财分钟 K {escape(facts.get("quote_trade_at") or "未核验")}；'
            '非逐笔成交时间</span></div></section>',
            '<h2>判断依据</h2><div class="analysis-checks">']
    for item in result["checks"]:
        body.append(f'<div class="analysis-check {"ok" if item["ok"] else "no"}">'
                    f'<span class="analysis-tag">{"通过" if item["ok"] else "未通过"}</span>'
                    f'<b>{escape(item["label"])}</b><p>{escape(item["detail"])}</p></div>')
    body.append("</div>")
    body.append("<h2>参考价格计划</h2>")
    if p:
        body.append('<div class="analysis-levels">'
                    f'<div><span>候选入场区 · {escape(p["setup"])}</span>'
                    f'<strong>{p["low"]:.2f}～{p["high"]:.2f}</strong></div>'
                    f'<div><span>风控止损</span><strong>{p["stop"]:.2f}</strong></div>'
                    f'<div><span>止盈一 · +10% 减半</span><strong>{p["tp1"]:.2f}</strong></div>'
                    f'<div><span>止盈二 · +20% 保护</span><strong>{p["tp2"]:.2f}</strong></div></div>'
                    f'<p class="analysis-hint">以候选区中位价 {p["reference"]:.2f} 计算新仓止损/止盈；'
                    '技术止损取 MA20 下方缓冲位与入场价 -7% 中较近者。'
                    '今日不可买或条件未通过时，该区间仅是研究情景，不是今日挂单价格。'
                    '只有在下一交易时段重新获取并核对全部数据后才可重新评估。'
                    '本系统仅检索媒体新闻，不自动核实公司正式公告；须自行核对。'
                    '跳空、滑点、T+1 可能使止损无法按标价成交。</p>')
    else:
        body.append("<p class='analysis-hint'>行情成交时刻或历史 K 未能核验，无法计算可靠的参考价格。</p>")
    if result["holding_levels"]:
        lv = result["holding_levels"]
        body.append('<p class="analysis-existing"><b>已有仓位另算：</b>'
                    f'成本 {escape(holding.get("cost"))}；持仓纪律止损 {lv["stop"]:.2f} '
                    f'({escape(lv["stop_src"])})，减半线 {lv["tp_half"]:.2f}，'
                    f'保护线 {lv["tp_protect"]:.2f}。新仓价位不覆盖这条纪律。</p>')
    body.append("<h2>技术与资金快照</h2><dl class='analysis-metrics'>")
    for label, value in (
        ("历史 K 至", tech.get("hist_date")),
        ("指标截至", tech.get("indicator_date")),
        ("MA10 / MA20", f'{number(tech.get("ma10"))} / {number(tech.get("ma20"))}'),
        ("RSI14 / 量比", f'{number(tech.get("rsi14"), digits=1)} / {number(tech.get("vr"))}'),
        ("前20日高", number(tech.get("high20_prev"))),
        ("板块", f'{sector.get("name", sector.get("industry", "未匹配"))} · '
         f'{sector.get("source", "暂无来源")}'),
        ("THS 全单净额", amount(ths_flow.get("net"))),
        ("东财大单净额", amount(em_flow.get("main_net"))),
        ("东财分钟累计", em_flow.get("last_time") or "未取得"),
    ):
        body.append(f'<div><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>')
    body.append("</dl><p class='analysis-hint'>THS 全单与东财大单的口径不同；方向冲突时不声称资金共振。"
                "快照采集时间不等于交易所最后一笔时间；未拿到当日收盘 K 时技术指标为盘中暂值。</p>")

    body.append(minute_chart_section(facts["code"], facts["name"]))

    body.append(announcement_section(facts.get("announcements")))

    body.append("<h2>消息面 · 原始标题</h2>")
    news = facts.get("news") or []
    if news:
        body.append("<ol class='analysis-news'>")
        for item in news:
            body.append('<li><span class="analysis-news-meta">'
                        f'{escape(item.get("time"))} · {escape(item.get("source"))} · '
                        f'关键词粗标：{escape(item.get("tag"))}</span>'
                        f'<div>{news_link(item)}</div></li>')
        body.append("</ol>")
    else:
        body.append("<p class='analysis-hint'>未取得可展示的个股新闻；不能据此推断没有公告或利空。</p>")
    body.append('<p class="analysis-hint">新闻来自东财采集的媒体条目，关键词标签可能误判；'
                '买入前请打开原文并核对公司正式公告，不能只凭标题判断事件真实性。</p>')
    if facts.get("source_errors"):
        errors = "；".join(f"{k}：{v}" for k, v in facts["source_errors"].items())
        body.append(f'<p class="analysis-hint">部分来源失败：{escape(errors)}。'
                    '缺口不会自动用历史值补成今日信号。</p>')
    body.append("<h2>仓位与风险</h2><ul class='analysis-risks'>")
    body.extend(f'<li>{escape(note)}</li>' for note in result["warnings"])
    body.append("</ul><p class='analysis-hint'>规则化辅助分析，不构成投资建议。"
                "若关注具体持仓，请到 <a href='/positions'>持仓管理</a> 核对真实成本与数量；"
                f'<a href="/stock?code={url_quote(facts["code"])}">查看个股详情</a>。</p>')
    body.append("</div>")
    return "".join(body)
