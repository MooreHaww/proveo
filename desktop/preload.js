// The only bridge between the page and the desktop app: four calls, nothing else.
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const str = (v) => (typeof v === "string" ? v : "");

contextBridge.exposeInMainWorld("ecoDesktop", {
  notify: (title, body) => ipcRenderer.invoke("eco:notify", str(title), str(body)),
  saveKey: (name, key, provider) => ipcRenderer.invoke("eco:saveKey", str(name), str(key), str(provider)),
  setKeyDisabled: (name, disabled) => ipcRenderer.invoke("eco:setKeyDisabled", str(name), disabled === true),
  removeKey: (name) => ipcRenderer.invoke("eco:removeKey", str(name)),
});
