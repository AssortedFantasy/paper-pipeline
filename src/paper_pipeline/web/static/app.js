/* ===== Paper Pipeline — Alpine + htmx app ===== */

function appShell() {
  const initialWorkerState = window._workerState || {};
  return {
    // ----- data -----
    papers: window._papersData || [],
    selected: [],
    activeCitekey: null,
    detailOpen: false,
    logCollapsed: false,
    logLines: [],
    workerRunning: Boolean(initialWorkerState.workerRunning),
    currentCitekey: initialWorkerState.currentCitekey || null,
    gpu: initialWorkerState.gpu || null,

    // ----- filter/sort state -----
    filterQuery: '',
    filterStatus: 'all',
    filterHasPdf: true,
    sortCol: 'citation_key',
    sortDir: 'asc',

    // ----- config -----
    config: {
      max_pages: 50,
      max_size_mb: 40,
      page_chunk_size: 0,
      model: '0.1.0-small',
      page_timeout_seconds: 1800,
      recompute: false,
      no_skipping: false,
    },

    // ----- lifecycle -----
    init() {
      this.connectSSE();
      this.refreshStatus();
      window.setInterval(() => this.refreshStatus(), 5000);
    },

    gpuLabel() {
      if (!this.gpu) return 'GPU unavailable';
      const prefix = this.gpu.device_count > 1 ? 'GPU ' + this.gpu.index : 'GPU';
      return prefix + ' ' + this.gpu.memory_used_mb + '/' + this.gpu.memory_total_mb + 'MB | ' + this.gpu.utilization_pct + '% | ' + this.gpu.temperature_c + 'C';
    },

    gpuTitle() {
      if (!this.gpu) return 'No NVIDIA GPU status available';
      const deviceLabel = this.gpu.device_count > 1 ? 'GPU ' + this.gpu.index + ': ' : '';
      return deviceLabel + this.gpu.name;
    },

    detailRunSummary() {
      return 'Uses current settings: max ' + this.config.max_pages + ' pages, max ' + this.config.max_size_mb + ' MB, model ' + this.config.model + '.';
    },

    refreshStatus() {
      fetch('/api/status')
        .then(r => r.json())
        .then(status => {
          this.workerRunning = Boolean(status.worker_running);
          this.currentCitekey = status.current_citekey || null;
          this.gpu = status.gpu || null;
        })
        .catch(() => {});
    },

    // ----- SSE -----
    connectSSE() {
      const src = new EventSource('/api/stream');
      src.addEventListener('job', (e) => {
        const d = JSON.parse(e.data);
        this.handleJobEvent(d);
      });
      src.addEventListener('connected', () => {
        this.appendLog('[connected to server]');
      });
      src.onerror = () => {
        this.appendLog('[SSE reconnecting...]');
      };
    },

    handleJobEvent(d) {
      const ts = d.timestamp ? d.timestamp.substring(11, 19) : '';
      switch (d.kind) {
        case 'paper_started':
          this.appendLog('[' + ts + '] \u25B6 Started: ' + d.citekey);
          this.updatePaperStatus(d.citekey, 'running');
          this.workerRunning = true;
          this.currentCitekey = d.citekey;
          break;
        case 'paper_completed':
          this.appendLog('[' + ts + '] \u2713 Completed: ' + d.citekey + ' \u2014 ' + d.message);
          this.updatePaperStatus(d.citekey, 'completed');
          this.updatePaperMeta(d.citekey, { last_run_iso: d.timestamp, error_message: null });
          break;
        case 'paper_failed':
          this.appendLog('[' + ts + '] \u2717 Failed: ' + d.citekey + ' \u2014 ' + d.message);
          this.updatePaperStatus(d.citekey, 'failed');
          this.updatePaperMeta(d.citekey, { last_run_iso: d.timestamp, error_message: d.message });
          break;
        case 'log_line':
          this.appendLog('[' + ts + '] ' + d.citekey + ': ' + d.message);
          break;
        case 'batch_done':
          this.appendLog('[' + ts + '] \u25A0 Batch complete.');
          this.workerRunning = false;
          this.currentCitekey = null;
          break;
        case 'batch_cancelled':
          this.appendLog('[' + ts + '] \u25A0 Batch cancelled.');
          this.workerRunning = false;
          this.currentCitekey = null;
          break;
      }
    },

    updatePaperStatus(citekey, status) {
      const p = this.papers.find(x => x.citation_key === citekey);
      if (p) p.transcription_status = status;
    },

    updatePaperMeta(citekey, patch) {
      const p = this.papers.find(x => x.citation_key === citekey);
      if (!p) return;
      Object.assign(p, patch);
    },

    appendLog(text) {
      this.logLines.push(text);
      if (this.logLines.length > 1000) {
        this.logLines = this.logLines.slice(-1000);
      }

      const el = document.getElementById('log-output');
      if (el) {
        el.textContent = this.logLines.join('\n') + '\n';
        el.scrollTop = el.scrollHeight;
      }
    },

    // ----- filter/sort -----
    isVisible(p) {
      if (this.filterHasPdf && !p.has_pdf) return false;
      if (this.filterStatus !== 'all' && p.transcription_status !== this.filterStatus) return false;
      if (this.filterQuery) {
        const q = this.filterQuery.toLowerCase();
        if (!p.citation_key.toLowerCase().includes(q) && !p.title.toLowerCase().includes(q)) return false;
      }
      return true;
    },

    visibleCount() {
      return this.papers.filter(p => this.isVisible(p)).length;
    },

    toggleSort(col) {
      if (this.sortCol === col) {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortCol = col;
        this.sortDir = 'asc';
      }
    },

    sortedPapers() {
      const col = this.sortCol;
      const asc = this.sortDir === 'asc';
      return [...this.papers].sort((a, b) => {
        let va = a[col], vb = b[col];
        if (va == null) va = '';
        if (vb == null) vb = '';
        if (typeof va === 'number' || typeof vb === 'number') {
          va = Number(va) || -1;
          vb = Number(vb) || -1;
          return asc ? va - vb : vb - va;
        }
        va = String(va).toLowerCase();
        vb = String(vb).toLowerCase();
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
        return 0;
      });
    },

    // ----- selection -----
    selectedKeys() {
      return this.selected;
    },

    toggleAll(checked) {
      if (checked) {
        this.selected = this.papers
          .filter(p => this.isVisible(p) && p.has_pdf)
          .map(p => p.citation_key);
      } else {
        this.selected = [];
      }
    },

    // ----- API -----
    transcribeSelected() {
      const keys = this.selectedKeys();
      if (keys.length === 0) return;
      this.startTranscription(keys);
    },

    transcribeAll() {
      fetch('/api/transcribe/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: this.config }),
      })
      .then(r => r.json())
      .then(preview => {
        if (!preview.queued) {
          this.appendLog('[No pending papers match the current page/size limits.]');
          return;
        }

        const sample = preview.queued_citekeys.slice(0, 8);
        const remaining = preview.queued_citekeys.length - sample.length;
        const lines = [
          'Queue ' + preview.queued + ' pending papers?',
          '',
          'Excluded: ' + preview.excluded_completed + ' completed, ' + preview.excluded_no_pdf + ' without PDF, ' + preview.excluded_page_cap + ' over page cap (' + preview.config.max_pages + '), ' + preview.excluded_size_cap + ' over size cap (' + preview.config.max_size_mb + ' MB).',
        ];

        if (sample.length > 0) {
          lines.push('');
          lines.push('First queued papers:');
          lines.push(sample.join('\n'));
          if (remaining > 0) {
            lines.push('');
            lines.push('...and ' + remaining + ' more.');
          }
        }

        if (window.confirm(lines.join('\n'))) {
          this.startTranscription([]);
        }
      })
      .catch(() => {
        this.appendLog('[Error] Failed to preview pending batch.');
      });
    },

    startTranscription(citekeys, configOverride = null) {
      const config = configOverride || this.config;
      fetch('/api/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ citekeys, config }),
      })
      .then(r => r.json())
      .then(data => {
        if (data.error) {
          this.appendLog('[Error] ' + data.error);
        } else {
          this.appendLog('[Started batch: ' + data.started + ' papers]');
        }
      });
    },

    stopBatch() {
      fetch('/api/transcribe/stop', { method: 'POST' })
        .then(r => r.json())
        .then(() => this.appendLog('[Stop requested]'));
    },

    rerunPaper(citekey, forceRecompute = false) {
      const config = { ...this.config };
      if (forceRecompute) {
        config.recompute = true;
      }
      this.startTranscription([citekey], config);
    },

    // ----- detail panel -----
    openDetail(citekey) {
      this.activeCitekey = citekey;
      this.detailOpen = true;
      htmx.ajax('GET', '/fragment/detail/' + citekey, {target: '#detail-content', swap: 'innerHTML'});
    },
  };
}
