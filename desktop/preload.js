// The only bridge between the page and the desktop app: three calls, nothing else.
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const str = (v) => (typeof v === "string" ? v : "");

contextBridge.exposeInMainWorld("ecoDesktop", {
  notify: (title, body) => ipcRenderer.invoke("eco:notify", str(title), str(body)),
  saveKey: (key) => ipcRenderer.invoke("eco:saveKey", str(key)),
  removeKey: () => ipcRenderer.invoke("eco:removeKey"),
});
