<template>
  <section class="page" data-module="monitorstation">
    <header class="page-head">
      <div>
        <h2>监测分站管理</h2>
        <p class="page-desc">统一台账导入导出口径，维护通信地址、接入传感器清单，并将分站状态同步到链路运行报表。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
        <button class="btn primary" type="button" @click="selectImportFile">导入台账文件</button>
        <button class="btn" type="button" @click="exportRows">导出监测分站清单</button>
        <input
          ref="importInput"
          class="file-input"
          type="file"
          accept=".csv,.json"
          @change="importRows"
        />
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
      <label class="filter-item">
        <span>分站状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div v-if="receipt" class="receipt-panel" :class="{ 'has-error': receipt.rejected_count > 0 }">
      <div class="receipt-head">
        <strong>导入回执</strong>
        <span>{{ receipt.message }}</span>
      </div>
      <table class="data-table receipt-table">
        <thead>
          <tr>
            <th>文件行号</th>
            <th>处理结果</th>
            <th>分站编号</th>
            <th>分站名称</th>
            <th>逐行说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in receipt.results" :key="line.row" :class="`receipt-${line.status}`">
            <td>第 {{ line.row }} 行</td>
            <td>{{ resultLabels[line.status] ?? line.status }}</td>
            <td>{{ line.data?.['分站编号'] || '—' }}</td>
            <td>{{ line.data?.['分站名称'] || '—' }}</td>
            <td>{{ line.message }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="table-block">
      <div class="block-title">
        <h3>分站台账明细</h3>
        <span>列表与详情读取同一次落库记录</span>
      </div>
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
              <button v-if="column === '分站编号'" class="link" type="button" @click="openDetail(row)">
                {{ row[column] ?? '—' }}
              </button>
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
            <td :colspan="columns.length + 1" class="empty-state">暂无监测分站数据，请下载模板后导入</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="table-block">
      <div class="block-title">
        <h3>链路运行报表</h3>
        <span>存量分站已按接入时间回填，状态变化自动同步</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in reportColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reportRows" :key="String(row.id)">
            <td v-for="column in reportColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!reportRows.length">
            <td :colspan="reportColumns.length" class="empty-state">暂无链路运行报表</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条监测分站记录，链路报表 {{ reportTotal }} 条</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="selectedDetail" class="modal-mask" @click.self="closeDetail">
      <section class="modal-card">
        <header class="modal-head">
          <h3>监测分站详情</h3>
          <button class="link" type="button" @click="closeDetail">关闭</button>
        </header>
        <dl class="detail-grid">
          <template v-for="column in columns" :key="column">
            <dt>{{ column }}</dt>
            <dd>{{ selectedDetail[column] ?? '—' }}</dd>
          </template>
        </dl>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request, API_BASE } from '@/api/client'

type Row = Record<string, string | number | null>
type ReceiptLine = {
  row: number
  status: 'imported' | 'duplicate' | 'rejected' | 'warning'
  message: string
  data?: Row | null
}
type Receipt = {
  message: string
  imported_count: number
  duplicate_count: number
  rejected_count: number
  results: ReceiptLine[]
}

const ENDPOINT = '/api/monitorstation'
const columns = ['分站编号', '分站名称', '所在位置', '通信地址', '接入传感器', '传感器数量', '信号强度', '后备电源', '分站状态', '接入时间']
const reportColumns = ['分站编号', '分站名称', '通信地址', '接入时间', '链路状态', '最近变化时间', '传感器数量']
const actions = ['通信排查', '切换供电', '办理停用']
const statuses = ['正常运行', '通信中断', '备用供电', '已停用']
const resultLabels: Record<ReceiptLine['status'], string> = {
  imported: '已入账',
  duplicate: '重复跳过',
  rejected: '整行退回',
  warning: '已入账（提示）',
}

const rows = ref<Row[]>([])
const reportRows = ref<Row[]>([])
const total = ref(0)
const reportTotal = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const receipt = ref<Receipt | null>(null)
const selectedDetail = ref<Row | null>(null)
const importInput = ref<HTMLInputElement | null>(null)
const filterFields = columns.slice(0, 3)
const stats = ref([
  { label: '正常分站', value: 0 },
  { label: '通信中断站', value: 0 },
  { label: '备用供电站', value: 0 },
])

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function selectImportFile() {
  importInput.value?.click()
}

function download(url: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function downloadTemplate() {
  download(`${API_BASE}${ENDPOINT}/template`)
}

function exportRows() {
  download(`${API_BASE}${ENDPOINT}/export?format=csv`)
}

async function importRows(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  errorMessage.value = ''
  receipt.value = null
  const form = new FormData()
  form.append('file', file)
  try {
    const response = await request(`${ENDPOINT}/import`, { method: 'POST', body: form })
    const payload = (await response.json()) as Receipt
    receipt.value = payload
    if (!response.ok && payload.rejected_count === payload.results.length) {
      throw new Error(payload.message || '台账文件未通过校验')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '台账导入失败'
  } finally {
    input.value = ''
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('监测分站详情读取失败')
    }
    selectedDetail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监测分站详情读取失败'
  }
}

function closeDetail() {
  selectedDetail.value = null
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('监测分站动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (payload.ok === false) {
      throw new Error(payload.message)
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监测分站操作失败'
  }
}

function updateStats() {
  const count = (status: string) => rows.value.filter((item) => item['分站状态'] === status).length
  stats.value = [
    { label: '正常分站', value: count('正常运行') },
    { label: '通信中断站', value: count('通信中断') },
    { label: '备用供电站', value: count('备用供电') },
  ]
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams({ ...filters.value, ...(statusFilter.value ? { status: statusFilter.value } : {}) }).toString()
  try {
    const [stationResponse, reportResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/link-reports?size=200`),
    ])
    if (!stationResponse.ok) {
      throw new Error('监测分站列表读取失败')
    }
    if (!reportResponse.ok) {
      throw new Error('链路运行报表读取失败')
    }
    const stationPayload = await stationResponse.json()
    const reportPayload = await reportResponse.json()
    rows.value = stationPayload.items ?? []
    total.value = stationPayload.total ?? rows.value.length
    reportRows.value = reportPayload.items ?? []
    reportTotal.value = reportPayload.total ?? reportRows.value.length
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监测分站数据读取失败'
  }
}

onMounted(reload)
</script>
