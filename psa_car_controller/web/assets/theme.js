/* The theme (light, dark or auto: the browser's) and the dashboard's section (the url's hash), both
   kept as attributes of <html> that the stylesheet reads. app.py sets them once before the page draws;
   this keeps them up to date and restyles the plotly figures, which can't read css variables. */
(function () {
  const KEY = 'psacc-theme'
  const CHOICES = ['light', 'dark', 'auto']
  const TABS = ['summary', 'trips', 'charging', 'map', 'control']
  const root = document.documentElement
  const media = window.matchMedia('(prefers-color-scheme: dark)')

  function choice () {
    let value = null
    try { value = window.localStorage.getItem(KEY) } catch (e) { /* storage blocked: follow the browser */ }
    return CHOICES.includes(value) ? value : 'auto'
  }

  function resolve (value) {
    return value === 'auto' ? (media.matches ? 'dark' : 'light') : value
  }

  function current () {
    return root.getAttribute('data-bs-theme') || resolve(choice())
  }

  function cssVar (name) {
    return window.getComputedStyle(root).getPropertyValue(name).trim()
  }

  function mapStyle () {
    return current() === 'dark' ? 'style-dark.json' : 'style.json'
  }

  /* the layout changes making a figure follow the theme, as plotly relayout keys */
  function layoutUpdate (layout) {
    const text = cssVar('--psacc-text')
    const muted = cssVar('--psacc-muted')
    const grid = cssVar('--psacc-border')
    const update = {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      'font.family': 'Barlow, system-ui, sans-serif',
      'font.color': muted,
      'title.font.color': text,
      'legend.font.color': muted,
      colorway: [cssVar('--psacc-accent'), cssVar('--psacc-drive')]
    }
    for (const axis of ['xaxis', 'yaxis']) {
      if (layout && layout[axis]) {
        update[axis + '.gridcolor'] = grid
        update[axis + '.zerolinecolor'] = grid
        update[axis + '.linecolor'] = grid
      }
    }
    if (layout && layout.map) {
      update['map.style'] = mapStyle()
    }
    return update
  }

  /* the colour a trace takes in the theme: bars and lines in the accent, scattered points in the
     driving colour; null leaves it (the map's position marker) */
  function traceColor (trace) {
    const mode = trace.mode || ''
    if (trace.type === 'histogram' || trace.type === 'bar') return cssVar('--psacc-accent')
    if (trace.type === 'scattermap') return mode.includes('lines') ? cssVar('--psacc-accent') : null
    if (!trace.type || trace.type === 'scatter' || trace.type === 'scattergl') {
      return mode.includes('lines') ? cssVar('--psacc-accent') : cssVar('--psacc-drive')
    }
    return null
  }

  function colorTrace (trace, color) {
    trace.marker = Object.assign({}, trace.marker, { color })
    if ((trace.mode || '').includes('lines')) trace.line = Object.assign({}, trace.line, { color })
  }

  /* a figure object (from a callback) made to follow the theme */
  function themeFigure (figure) {
    if (!figure || !figure.layout) return figure
    ;(figure.data || []).forEach(trace => {
      const color = traceColor(trace)
      if (color) colorTrace(trace, color)
    })
    const update = layoutUpdate(figure.layout)
    for (const [path, value] of Object.entries(update)) {
      const keys = path.split('.')
      let node = figure.layout
      keys.slice(0, -1).forEach(key => {
        if (typeof node[key] !== 'object' || node[key] === null) node[key] = {}
        node = node[key]
      })
      node[keys[keys.length - 1]] = value
    }
    return figure
  }

  function themed (plot) {
    const layout = plot.layout || {}
    const fontOk = layout.font && layout.font.color === cssVar('--psacc-muted')
    const mapOk = !layout.map || layout.map.style === mapStyle()
    const tracesOk = (plot.data || []).every(trace => {
      const color = traceColor(trace)
      return !color || (trace.marker && trace.marker.color === color)
    })
    return fontOk && mapOk && tracesOk
  }

  /* restyles the figures drawn with another theme, or with none (server-side figures) */
  function restylePlots () {
    if (!window.Plotly) return
    document.querySelectorAll('.js-plotly-plot').forEach(plot => {
      if (plot.layout && !themed(plot)) {
        (plot.data || []).forEach((trace, index) => {
          const color = traceColor(trace)
          if (!color) return
          const update = { 'marker.color': color }
          if ((trace.mode || '').includes('lines')) update['line.color'] = color
          window.Plotly.restyle(plot, update, [index])
        })
        window.Plotly.relayout(plot, layoutUpdate(plot.layout))
      }
    })
  }

  function syncButtons () {
    const value = choice()
    document.querySelectorAll('.psacc-theme button[data-choice]').forEach(button => {
      button.setAttribute('aria-checked', String(button.dataset.choice === value))
    })
    // toggle buttons whose state a callback sets as a class (the summary's period)
    document.querySelectorAll('.psacc-seg').forEach(button => {
      button.setAttribute('aria-pressed', String(button.classList.contains('active')))
    })
  }

  function applyTheme (value) {
    root.setAttribute('data-theme-choice', value)
    root.setAttribute('data-bs-theme', resolve(value))
    syncButtons()
    restylePlots()
  }

  function applyTab () {
    if (!document.getElementById('panel-summary')) {
      root.removeAttribute('data-tab') // not the dashboard
      return
    }
    const hash = window.location.hash.replace('#', '')
    const tab = TABS.includes(hash) ? hash : 'summary'
    const changed = root.getAttribute('data-tab') !== tab
    root.setAttribute('data-tab', tab)
    document.querySelectorAll('.psacc-nav a[data-tab]').forEach(link => {
      if (link.dataset.tab === tab) link.setAttribute('aria-current', 'page')
      else link.removeAttribute('aria-current')
    })
    if (changed) {
      // a graph drawn while hidden has no size until the window resizes
      window.setTimeout(() => window.dispatchEvent(new Event('resize')), 30)
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('.psacc-theme button[data-choice]')
    if (!button) return
    try { window.localStorage.setItem(KEY, button.dataset.choice) } catch (e) { /* kept for this page only */ }
    applyTheme(button.dataset.choice)
  })

  media.addEventListener('change', () => {
    if (choice() === 'auto') applyTheme('auto')
  })

  window.addEventListener('hashchange', applyTab)

  // chosen in another page of the app (the control section is one, in a frame)
  window.addEventListener('storage', event => {
    if (event.key === KEY) applyTheme(choice())
  })

  // dash renders the page after this runs, and re-renders parts of it
  let pending = false
  new window.MutationObserver(() => {
    if (pending) return
    pending = true
    window.requestAnimationFrame(() => {
      pending = false
      syncButtons()
      applyTab()
      restylePlots()
    })
  }).observe(document.body || root, { childList: true, subtree: true })

  window.psaccTheme = { current, themeFigure, mapStyle }
  applyTheme(choice())
}())
