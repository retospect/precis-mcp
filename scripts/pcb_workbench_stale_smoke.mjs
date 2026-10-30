#!/usr/bin/env node
// Smoke for the workbench's stale-board 409 handling (gr519469): the
// <object> paints a text 409 raw in engines that give an error body a
// document, and hides it behind the fallback content in engines that
// don't. The 'board changed — reload' control must surface either way —
// sniffed from the loaded document when there is one, probed from the
// route's status when there isn't. Run by tests/test_pcb_workbench_js.py
// (skips without node). Exits non-zero on the first failed assertion.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const SRC = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../src/precis_web/static/pcb-workbench.js');

const flush = () => new Promise((r) => setTimeout(r, 0));

function element(id) {
  const listeners = {};
  const classes = new Set(id === 'pcb-board-stale' ? ['hidden'] : []);
  return {
    id,
    listeners,
    attrs: {},
    dataset: {},
    hidden: false,
    disabled: false,
    textContent: '',
    contentDocument: null,
    classList: {
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      contains: (c) => classes.has(c),
    },
    addEventListener: (ev, fn) => (listeners[ev] ||= []).push(fn),
    fire: (ev) => (listeners[ev] || []).forEach((fn) => fn()),
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k] ?? null; },
    querySelector: () => ({ textContent: '' }),
  };
}

function world() {
  const els = {};
  for (const id of [
    'pcb-workbench', 'pcb-pane-schematic', 'pcb-pane-layout', 'pcb-divider',
    'pcb-flip', 'pcb-height', 'pcb-board-loading', 'pcb-board-stale',
    'pcb-board-reload', 'pcb-board-object', 'pcb-schematic-object',
  ]) els[id] = element(id);
  const w = { els, fetches: [], res: { status: 200 }, reloaded: false };
  vm.runInContext(readFileSync(SRC, 'utf8'), vm.createContext({
    document: { getElementById: (id) => els[id] ?? null },
    fetch: (url) => { w.fetches.push(url); return Promise.resolve(w.res); },
    MutationObserver: class { observe() {} },
    localStorage: { getItem: () => null, setItem() {} },
    // The loading note's timer is not under test; scheduling it for real
    // would only hold the process open.
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout,
    console,
    window: { location: { reload: () => { w.reloaded = true; } } },
  }));
  return w;
}

// A readable text body is enough on its own — no probe needed.
{
  const w = world();
  const obj = w.els['pcb-board-object'];
  obj.setAttribute('data', '/pcb/x/board.svg?shown=OLD');
  obj.contentDocument = {
    documentElement: { localName: 'html' },
    body: { textContent: 'shown PCB changed; reload' },
  };
  obj.fire('load');
  assert.equal(w.els['pcb-board-stale'].classList.contains('hidden'), false);
  await flush();
  assert.equal(w.fetches.length, 0);
}

// A real svg load keeps the control hidden and never probes.
{
  const w = world();
  const obj = w.els['pcb-board-object'];
  obj.setAttribute('data', '/pcb/x/board.svg?shown=TOK');
  obj.contentDocument = { documentElement: { localName: 'svg' } };
  obj.fire('load');
  assert.equal(w.els['pcb-board-stale'].classList.contains('hidden'), true);
  await flush();
  assert.equal(w.fetches.length, 0);
}

// An 'error' load exposes no document — the route's status decides: a
// 409 still earns the reload control.
{
  const w = world();
  const obj = w.els['pcb-board-object'];
  obj.setAttribute('data', '/pcb/x/board.svg?shown=OLD');
  w.res = { status: 409 };
  obj.fire('error');
  await flush();
  assert.deepEqual(w.fetches, ['/pcb/x/board.svg?shown=OLD']);
  assert.equal(w.els['pcb-board-stale'].classList.contains('hidden'), false);
}

// A settled load with nothing readable probes too — but only a 409
// counts, so a plain 404 leaves the control hidden.
{
  const w = world();
  const obj = w.els['pcb-board-object'];
  obj.setAttribute('data', '/pcb/x/board.svg?shown=OLD');
  w.res = { status: 404 };
  obj.fire('load');
  await flush();
  assert.equal(w.fetches.length, 1);
  assert.equal(w.els['pcb-board-stale'].classList.contains('hidden'), true);
}

// A probe resolving after the object's data moved on is dropped — the
// newer request owns the pane.
{
  const w = world();
  const obj = w.els['pcb-board-object'];
  obj.setAttribute('data', '/pcb/x/board.svg?shown=OLD');
  w.res = { status: 409 };
  obj.fire('error');
  obj.setAttribute('data', '/pcb/x/board.svg?shown=NEW');
  await flush();
  assert.equal(w.els['pcb-board-stale'].classList.contains('hidden'), true);
}
