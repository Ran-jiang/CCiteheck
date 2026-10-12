(function () {
  "use strict"

  var tabs = {
    mac: document.getElementById("tab-mac"),
    windows: document.getElementById("tab-windows"),
  }
  var panels = {
    mac: document.getElementById("panel-mac"),
    windows: document.getElementById("panel-windows"),
  }

  function select(name) {
    Object.keys(tabs).forEach(function (key) {
      var active = key === name
      tabs[key].classList.toggle("is-active", active)
      tabs[key].setAttribute("aria-selected", String(active))
      panels[key].hidden = !active
    })
  }

  tabs.mac.addEventListener("click", function () { select("mac") })
  tabs.windows.addEventListener("click", function () { select("windows") })

  var ua = navigator.userAgent || ""
  var platform = navigator.platform || ""
  var isMac = /Mac|iPod|iPhone|iPad/.test(ua) || /Mac/.test(platform)
  select(isMac ? "mac" : "windows")
})()
