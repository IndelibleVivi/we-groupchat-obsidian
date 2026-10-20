"use strict";

const {Plugin, MarkdownView, parseYaml} = require("obsidian");
const fs = require("node:fs").promises;
const constants = require("node:fs").constants;
const path = require("node:path");
const crypto = require("node:crypto");

const ROOT = "WGO";
const SCHEMA = "wgo.reader-visible-open.v1";
const MAX_NOTE_BYTES = 2 * 1024 * 1024;

function sha256(value) {
    return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function visibleView(app) {
    // getActiveFile may return a recently active file, including a hidden pane.
    const view = app.workspace.getActiveViewOfType(MarkdownView);
    if (!(view instanceof MarkdownView) || view.leaf?.isDeferred || !view.file) return null;
    const element = view.containerEl;
    const doc = element?.ownerDocument;
    if (!element?.isConnected || doc?.visibilityState !== "visible" || !doc.hasFocus()) return null;
    const rect = element.getBoundingClientRect();
    const style = doc.defaultView?.getComputedStyle(element);
    if (!rect || rect.width <= 0 || rect.height <= 0 || !style ||
        style.display === "none" || style.visibility !== "visible") return null;
    return view;
}

function frontmatter(text) {
    if (typeof text !== "string" || Buffer.byteLength(text, "utf8") > MAX_NOTE_BYTES) return null;
    if (!text.startsWith("---\n") && !text.startsWith("---\r\n")) return null;
    const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
    if (!match) return null;
    const fields = parseYaml(match[1]);
    if (!fields || typeof fields !== "object") return null;
    return fields;
}

function identity(text) {
    const fields = frontmatter(text);
    if (!fields) return null;
    const id = fields.wgo_id;
    const generation = fields.wgo_generation;
    const revision = fields.wgo_content_revision;
    if (typeof id !== "string" || !id || id.length > 200 || /^(?:source:|view:|control:|query-history:)/.test(id) ||
        typeof generation !== "string" || !generation || generation.length > 200 ||
        !Number.isSafeInteger(revision) || revision <= 0) return null;
    return {item_id: id, generation_id: generation, content_revision: revision};
}

class ReadingPresentation {
    constructor(app) {
        this.app = app;
        this.elements = new Set();
    }

    sync() {
        const current = new Set();
        for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
            const view = leaf.view;
            if (!(view instanceof MarkdownView) || leaf.isDeferred || !view.containerEl) continue;
            const element = view.containerEl;
            current.add(element);
            let fields = null;
            try {
                const relative = view.file?.path;
                if (typeof relative === "string" && relative.startsWith(ROOT + "/") &&
                    relative.endsWith(".md") && relative.length <= 1028 &&
                    !relative.split("/").some(part => !part || part === "." || part === ".." || part === ".wgo")) {
                    fields = frontmatter(view.getViewData());
                }
            } catch (_) { /* Presentation never changes note or read-event state. */ }
            const managed = !!fields && typeof fields.wgo_id === "string" &&
                fields.wgo_id.length > 0 && fields.wgo_id.length <= 200 &&
                typeof fields.wgo_generation === "string" && fields.wgo_generation.length > 0 &&
                fields.wgo_generation.length <= 200 && Number.isSafeInteger(fields.wgo_content_revision) &&
                fields.wgo_content_revision > 0;
            element.classList.toggle("wgo-reader-page", managed);
            element.classList.toggle("wgo-reader-machine-only", managed &&
                Object.keys(fields).every(key => key.startsWith("wgo_")));
        }
        for (const element of this.elements) {
            if (!current.has(element)) this.clear(element);
        }
        this.elements = current;
    }

    clear(element) {
        element.classList.remove("wgo-reader-page", "wgo-reader-machine-only");
    }

    unload() {
        for (const element of this.elements) this.clear(element);
        this.elements.clear();
    }
}

async function regularPath(base, relative, {directory = false, create = false} = {}) {
    if (typeof relative !== "string" || relative.includes("\\") || relative.includes("\0") ||
        path.isAbsolute(relative) || relative.split("/").some(p => !p || p === "." || p === "..")) {
        throw new Error("reader_path_invalid");
    }
    const resolved = path.resolve(base);
    // Reject symlinks in the base as well as its managed descendants.
    const root = path.parse(resolved).root;
    let cursor = root;
    for (const segment of resolved.slice(root.length).split(path.sep).filter(Boolean)) {
        cursor = path.join(cursor, segment);
        const info = await fs.lstat(cursor);
        if (info.isSymbolicLink() || !info.isDirectory()) throw new Error("reader_path_unsafe");
    }
    const parts = relative.split("/");
    for (let index = 0; index < parts.length; index++) {
        cursor = path.join(cursor, parts[index]);
        const isDirectory = index < parts.length - 1 || directory;
        let info;
        try {
            info = await fs.lstat(cursor);
        } catch (error) {
            if (error.code !== "ENOENT" || !create || !isDirectory) throw error;
            try { await fs.mkdir(cursor, {mode: 0o700}); }
            catch (created) { if (created.code !== "EEXIST") throw created; }
            info = await fs.lstat(cursor);
        }
        if (info.isSymbolicLink() || (isDirectory ? !info.isDirectory() : !info.isFile())) {
            throw new Error("reader_path_unsafe");
        }
        if (isDirectory && parts.slice(0, index + 1).includes(".wgo") && (info.mode & 0o077)) {
            throw new Error("reader_directory_not_private");
        }
    }
    return cursor;
}

async function readNote(note) {
    const handle = await fs.open(note, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
        const info = await handle.stat();
        if (!info.isFile() || info.size > MAX_NOTE_BYTES) return null;
        const buffer = Buffer.alloc(MAX_NOTE_BYTES + 1);
        let length = 0;
        while (length < buffer.length) {
            const result = await handle.read(buffer, length, buffer.length - length, length);
            if (result.bytesRead === 0) break;
            length += result.bytesRead;
        }
        return length <= MAX_NOTE_BYTES ? buffer.subarray(0, length).toString("utf8") : null;
    } finally { await handle.close(); }
}

class VisibleOpenWriter {
    constructor(app, {now = () => Date.now() / 1000, random = () => crypto.randomBytes(16).toString("hex")} = {}) {
        this.app = app;
        this.now = now;
        this.random = random;
        this.last = null;
        this.epoch = 0;
    }

    reset() {
        this.last = null;
        this.epoch++;
    }

    async observe() {
        if (process.platform === "win32") return false; // No Windows DACL adapter yet.
        const view = visibleView(this.app);
        if (!view) { this.reset(); return false; }
        const file = view.file;
        const relative = file.path;
        if (typeof relative !== "string" || !relative.startsWith(ROOT + "/") ||
            relative.includes("/.wgo/") || !relative.endsWith(".md") || relative.length > 1028) return false;
        const text = view.getViewData();
        if (typeof text !== "string" || Buffer.byteLength(text, "utf8") > MAX_NOTE_BYTES) return false;
        const fields = identity(text);
        if (!fields) return false;
        const epoch = this.epoch;
        const signature = sha256(text);
        const key = JSON.stringify([fields, relative, signature]);
        if (this.last === key) return false;
        const base = this.app.vault.adapter.getBasePath();
        const note = await regularPath(base, relative);
        const disk = await readNote(note);
        // Unsaved editor contents or a replaced projection are not an exact
        // displayed canonical file version. A later visible event may retry.
        if (disk !== text) return false;
        const inbox = await regularPath(base, ROOT + "/.wgo/reader-events", {directory: true, create: true});
        const stillVisible = () => epoch === this.epoch && visibleView(this.app) === view &&
            view.file === file && view.file.path === relative && view.getViewData() === text;
        if (!stillVisible()) return false;
        const id = this.random();
        if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("reader_event_id_invalid");
        const event = {...fields, schema: SCHEMA, event_id: id, path: relative.slice(ROOT.length + 1),
            file_sha256: signature, observed_at: this.now(), origin: "obsidian_visible_open", visible: true};
        const encoded = JSON.stringify(event);
        if (Buffer.byteLength(encoded, "utf8") > 8192) throw new Error("reader_event_too_large");
        const temporary = path.join(inbox, id + ".tmp");
        const final = path.join(inbox, id + ".json");
        const output = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
        try {
            await output.writeFile(encoded, {encoding: "utf8"});
            await output.sync();
        } finally {
            await output.close();
        }
        // A focus change while local IO was pending invalidates this event.
        // Its private .tmp remains inert; only final JSON is consumed.
        if (!stillVisible()) return false;
        await regularPath(base, ROOT + "/.wgo/reader-events", {directory: true});
        await regularPath(base, relative);
        if (await readNote(note) !== text || !stillVisible()) return false;
        try { await fs.lstat(final); throw new Error("reader_event_collision"); }
        catch (error) { if (error.code !== "ENOENT") throw error; }
        await fs.rename(temporary, final);
        const directory = await fs.open(inbox, constants.O_RDONLY);
        try { await directory.sync(); } finally { await directory.close(); }
        this.last = key;
        return true;
    }
}

class WGOReaderObserver extends Plugin {
    onload() {
        this.writer = new VisibleOpenWriter(this.app);
        this.presentation = new ReadingPresentation(this.app);
        this.pending = false;
        this.running = false;
        this.stopped = false;
        this.scheduleEpoch = 0;
        this.frameWait = null;
        this.documents = new WeakSet();
        const schedule = (reset = false) => {
            if (this.stopped) return;
            if (reset) this.writer.reset();
            this.presentation.sync();
            this.pending = true;
            this.scheduleEpoch++;
            this.cancelFrame();
            this.bindDocument(schedule);
            if (!this.running) void this.flush();
        };
        this.registerEvent(this.app.workspace.on("active-leaf-change", () => schedule(true)));
        this.registerEvent(this.app.workspace.on("file-open", () => schedule(true)));
        this.registerEvent(this.app.workspace.on("layout-change", () => schedule()));
        this.registerEvent(this.app.vault.on("modify", file => {
            if (visibleView(this.app)?.file === file) schedule();
            else if (file.path?.startsWith(ROOT + "/")) this.presentation.sync();
        }));
        this.app.workspace.onLayoutReady(() => schedule());
    }

    bindDocument(schedule) {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const doc = view?.containerEl?.ownerDocument;
        if (!doc || this.documents.has(doc)) return;
        this.documents.add(doc);
        this.registerDomEvent(doc, "visibilitychange", () => schedule(true));
        this.registerDomEvent(doc.defaultView, "focus", () => schedule(true));
        this.registerDomEvent(doc.defaultView, "blur", () => schedule(true));
    }

    cancelFrame() {
        this.frameWait?.cancel();
    }

    waitForFrame(doc) {
        const win = doc?.defaultView;
        if (doc?.visibilityState !== "visible" || typeof win?.requestAnimationFrame !== "function") {
            return Promise.resolve(true);
        }
        return new Promise(resolve => {
            const waiter = {id: null, cancel: null};
            const finish = ready => {
                if (this.frameWait !== waiter) return;
                this.frameWait = null;
                resolve(ready);
            };
            waiter.cancel = () => {
                if (this.frameWait !== waiter) return;
                try { win.cancelAnimationFrame?.(waiter.id); }
                catch (_) { /* A detached window need not accept cancellation. */ }
                finish(false);
            };
            this.frameWait = waiter;
            try { waiter.id = win.requestAnimationFrame(() => finish(true)); }
            catch (_) { finish(false); }
        });
    }

    async flush() {
        this.running = true;
        try {
            while (this.pending && !this.stopped) {
                this.pending = false;
                const epoch = this.scheduleEpoch;
                const doc = this.app.workspace.getActiveViewOfType(MarkdownView)?.containerEl?.ownerDocument;
                const ready = await this.waitForFrame(doc);
                if (this.stopped) break;
                if (!ready || epoch !== this.scheduleEpoch) continue;
                if (this.app.workspace.getActiveViewOfType(MarkdownView)?.containerEl?.ownerDocument !== doc) {
                    this.pending = true;
                    continue;
                }
                try { await this.writer.observe(); }
                catch (_) { /* Fail closed, without logging paths or note text. */ }
            }
        } finally { this.running = false; }
    }

    onunload() {
        this.stopped = true;
        this.pending = false;
        this.scheduleEpoch++;
        this.cancelFrame();
        this.writer.reset();
        this.presentation.unload();
    }
}

module.exports = WGOReaderObserver;
module.exports._test = {VisibleOpenWriter, visibleView, identity, ReadingPresentation};
