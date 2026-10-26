// Fake Quickshell objects for the Backend.run plumbing. Quickshell's Process
// has no terminate(): the kill path is `proc.running = false`, so the fake
// counts exactly those transitions; the Timer fake records the watchdog's
// restart/stop calls.
export function fakeProc() {
  return {
    _v: false,
    killCount: 0,
    set running(v) { if (v === false && this._v === true) this.killCount++; this._v = v },
    get running() { return this._v }
  }
}

export function fakeTimer() {
  return { interval: 0, restarted: false, stopped: false,
    restart() { this.restarted = true }, stop() { this.stopped = true } }
}
