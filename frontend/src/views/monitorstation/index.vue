<template>
  <section class="page" data-module="monitorstation">
    <header class="page-head">
      <div>
        <h2>监测分站管理</h2>
        <p class="page-desc">通信地址与接入传感器清单从设备台账导入：先校验再落库，重复导入只认第一次，回执逐行说明。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记监测分站</button>
        <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
        <button class="btn" type="button" @click="triggerImport">导入台账文件</button>
        <button class="btn" type="button" @click="exportRows">导出台账明细</button>
        <input ref="fileInput" type="file" accept=".csv,.json" hidden @change="importFile" />
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <button v-if="column === '分站编号'" class="link" type="button" @click="showDetail(row)">{{ row[column] ?? '—' }}</button>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无监测分站数据，可先导入台账或登记监测分站</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条监测分站记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="receipt" class="receipt-panel">
      <h3>导入回执</h3>
      <p class="page-desc">{{ receipt.message }}</p>
      <table class="data-table">
        <thead>
          <tr><th>文件行号</th><th>分站编号</th><th>分站名称</th><th>结果</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in receipt.rows" :key="`${item.line}-${item.分站编号}`">
            <td>第 {{ item.line }} 行</td>
            <td>{{ item.分站编号 || '—' }}</td>
            <td>{{ item.分站名称 || '—' }}</td>
            <td :class="receiptClass(item.result)">{{ item.result }}</td>
            <td>{{ item.message }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="report-panel">
      <h3>监测分站链路运行报表</h3>
      <p class="page-desc">分站状态变化即时同步；存量分站按接入时间回填。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in reportColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reportRows" :key="`report-${String(row.分站ID)}`">
            <td v-for="column in reportColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!reportRows.length">
            <td :colspan="reportColumns.length" class="empty-state">暂无链路运行数据</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal-card">
        <h3>分站详情</h3>
        <dl class="detail-list">
          <template v-for="column in detailColumns" :key="column">
            <dt>{{ column }}</dt>
            <dd>{{ detail[column] ?? '—' }}</dd>
          </template>
        </dl>
        <div class="modal-actions">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Receipt = {
  total: number
  imported: number
  skipped: number
  rejected: number
  rows: { line: number; 分站编号: string; 分站名称: string; result: string; message: string }[]
  message: string
}

const ENDPOINT = '/api/monitorstation'
const columns = ["分站编号", "分站名称", "所在位置", "通信地址", "接入传感器", "信号强度", "后备电源", "分站状态"]
const detailColumns = ["id", ...columns, "接入时间"]
const reportColumns = ["分站编号", "分站名称", "所在位置", "通信地址", "接入传感器", "接入时间", "链路状态", "更新时间"]
const actions = ["通信排查", "切换供电", "办理停用"]
const statuses = ["正常运行", "通信中断", "备用供电", "已停用"]
const stats = ref([{ label: "正常分站", value: 0 }, { label: "通信中断站", value: 0 }, { label: "备用供电站", value: 0 }])

const rows = ref<Row[]>([])
const reportRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const fileInput = ref<HTMLInputElement | null>(null)
const receipt = ref<Receipt | null>(null)
const detail = ref<Row | null>(null)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function downloadTemplate() {
  window.open(`${ENDPOINT}/template`, '_blank')
}

function triggerImport() {
  errorMessage.value = ''
  fileInput.value?.click()
}

async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  errorMessage.value = ''
  try {
    const content = await file.text()
    const response = await request(`${ENDPOINT}/import`, {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, content }),
    })
    if (!response.ok) {
      const payload = (await response.json().catch(() => null)) as { detail?: string } | null
      throw new Error(payload?.detail || `导入失败（HTTP ${response.status}），整批未落库`)
    }
    receipt.value = (await response.json()) as Receipt
    await reload()
  } catch (error) {
    receipt.value = null
    errorMessage.value = error instanceof Error ? error.message : '台账文件导入失败'
  }
}

function receiptClass(result: string) {
  if (result === '入账') return 'receipt-ok'
  if (result === '跳过') return 'receipt-skip'
  return 'receipt-bad'
}

function openCreate() {
  errorMessage.value = '监测分站登记入口尚未接入审批流'
}

async function showDetail(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('分站详情读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分站详情读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('监测分站动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监测分站操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResponse, reportResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/link-report`),
    ])
    if (!listResponse.ok) {
      throw new Error('监测分站列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = rows.value.filter((row) => row.分站状态 === statuses[0]).length
    stats.value[1].value = rows.value.filter((row) => row.分站状态 === statuses[1]).length
    stats.value[2].value = rows.value.filter((row) => row.分站状态 === statuses[2]).length

    if (!reportResponse.ok) {
      throw new Error('链路运行报表读取失败')
    }
    const reportPayload = await reportResponse.json()
    reportRows.value = reportPayload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监测分站列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.receipt-panel,
.report-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.receipt-panel h3,
.report-panel h3 { margin: 0 0 8px; font-size: 15px; }
.receipt-ok { color: #067647; font-weight: 600; }
.receipt-skip { color: #b54708; }
.receipt-bad { color: #b42318; font-weight: 600; }
.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45); display: flex; align-items: center; justify-content: center; }
.modal-card { background: #fff; border-radius: 8px; padding: 18px 22px; width: 560px; max-height: 80vh; overflow: auto; }
.modal-card h3 { margin: 0 0 12px; }
.detail-list { display: grid; grid-template-columns: 120px 1fr; gap: 6px 12px; margin: 0; }
.detail-list dt { color: var(--muted); font-size: 13px; }
.detail-list dd { margin: 0; font-size: 13px; }
.modal-actions { margin-top: 14px; text-align: right; }
</style>
