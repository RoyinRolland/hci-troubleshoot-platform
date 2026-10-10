import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import KbdReviewView from '../KbdReviewView.vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
  window.localStorage.clear()
})

/**
 * 被拒候选专家处置（复核忽略 / 撤销处置）：
 * 无信号案例只有把全部被拒候选显式处置后才能按「仅案例推荐」发布。
 * 处置是服务端单对象 PATCH（V-007）：只携带稳定候选 ID + 说明 + 操作人，
 * 审计快照（candidate/reason）由服务端原样保留。
 */
const pendingCandidate = {
  candidate: { id: 'sig_002', acquire: { tool: 'qfk_network', args: {} } },
  reason_code: 'run_failed',
  reason: 'keyword Matcher 的 match.pattern 无法从 provenance.evidence 逐字追溯',
}
const dismissedCandidate = {
  candidate: { id: 'sig_003', acquire: { tool: 'qfk_network', args: {} } },
  reason_code: 'run_failed',
  reason: "判定器校验失败：'gte' 非法",
  expert_dismissal: {
    reviewed_by: '42',
    reviewed_at: '2026-10-10T08:00:00+00:00',
    note: '已确认为纯文本知识，候选无恢复价值',
  },
}

function detailEntryFixture(candidates: unknown[]) {
  return {
    id: 2468,
    support_id: '43596',
    title: '跨集群迁移端口连通性检查',
    status: 'draft',
    content_md: '',
    images_json: [],
    metadata: {},
    category_id: 'net-001',
    lock_version: 3,
    signals_json: { schema_version: 2, signals: [], rejected_candidates: candidates },
  }
}

function jsonResponse(payload: unknown) {
  return { ok: true, status: 200, headers: new Headers(), json: async () => payload }
}

function stubFetch(patchResponse: Record<string, unknown>) {
  const fetchMock = vi.fn(async (url: any, init?: any) => {
    const target = String(url)
    if (init?.method === 'PATCH') return jsonResponse(patchResponse)
    // review-signals 必须返回完整审查响应形态，否则详情对话框模板读取 issues 崩溃
    if (target.includes('/review-signals')) {
      return jsonResponse({
        kbd_id: 2468,
        working_revision_id: null,
        lock_version: 4,
        status: 'ok',
        publishable: true,
        runtime_verified: false,
        runtime_verification_required: false,
        signal_review: { feature: 'expert', status: 'passed', issues: [] },
        error_count: 0,
        warning_count: 0,
        issues: [],
        platform_status: [],
      })
    }
    if (target.includes('/revisions')) return jsonResponse({})
    // fetchPending 以 data.entries 赋值列表（KbdReviewView.vue:635），mock 必须对齐真实形态
    return jsonResponse({ entries: [], total: 0 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function mountView(candidates: unknown[], patchResponse: Record<string, unknown> = {}) {
  const fetchMock = stubFetch(patchResponse)
  const wrapper = mount(KbdReviewView, { global: { plugins: [ElementPlus] }, attachTo: document.body })
  await flushPromises()
  const vm = wrapper.vm as any
  vm.detailEntry = detailEntryFixture(candidates)
  vm.detailDialogVisible = true
  await nextTick()
  await flushPromises()
  return { wrapper, vm, fetchMock }
}

describe('被拒候选专家处置（复核忽略 / 撤销处置）', () => {
  it('待复核候选显示复核忽略入口；已处置候选展示处置记录与撤销入口', async () => {
    const { wrapper } = await mountView([pendingCandidate, dismissedCandidate])
    const rendered = document.body.textContent || ''
    // 待复核项：处置按钮 + 状态标签
    expect(rendered).toContain('复核忽略')
    expect(rendered).toContain('待复核')
    // 已处置项：状态标签 + 操作人 + 处置说明 + 撤销入口
    expect(rendered).toContain('已复核忽略')
    expect(rendered).toContain('操作人 42')
    expect(rendered).toContain('已确认为纯文本知识，候选无恢复价值')
    expect(rendered).toContain('撤销处置')
    // 审计快照本体（拒绝原因）始终可见
    expect(rendered).toContain('无法从 provenance.evidence 逐字追溯')
    // 顶部提示给出两条出路
    expect(rendered).toContain('恢复并编辑')
    wrapper.unmount()
  })

  it('全部处置后顶部提示切换为可按「仅案例推荐」发布', async () => {
    const { wrapper } = await mountView([dismissedCandidate])
    const rendered = document.body.textContent || ''
    expect(rendered).toContain('均已复核处置')
    expect(rendered).toContain('仅案例推荐')
    // 面板内不得再出现「待复核」标签（页面其他区域的同名状态文案不在断言范围）
    const panel = document.querySelector('.rejected-candidates')
    expect(panel).toBeTruthy()
    expect(panel!.textContent || '').not.toContain('待复核')
    expect(panel!.textContent || '').toContain('已复核忽略')
    wrapper.unmount()
  })

  it('复核忽略提交单对象 PATCH：稳定候选 ID + 处置说明 + 操作人 + 受控原因码', async () => {
    const updatedDoc = {
      schema_version: 2,
      signals: [],
      rejected_candidates: [dismissedCandidate],
    }
    const { wrapper, vm, fetchMock } = await mountView(
      [pendingCandidate],
      { signals_json: updatedDoc, lock_version: 4 },
    )

    vm.currentUser = 42
    vm.dismissRejectedCandidateId = 'sig_002'
    vm.dismissRejectedNote = '案例确认为纯文本知识，候选无恢复价值'
    await vm.submitDismissRejectedCandidate()
    await flushPromises()

    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patchCall).toBeTruthy()
    const body = JSON.parse(String(patchCall![1].body))
    expect(body.dismiss_rejected_candidate_id).toBe('sig_002')
    expect(body.dismiss_rejected_note).toBe('案例确认为纯文本知识，候选无恢复价值')
    expect(body.reviewer_id).toBe(42)
    expect(body.lock_version).toBe(3)
    expect(body.change_annotations).toEqual([
      {
        path: '/signals_json/rejected_candidates',
        reason_code: 'rejected_candidate_dismissed',
        note: '案例确认为纯文本知识，候选无恢复价值',
      },
    ])
    // 不得夹带整稿 signals_json（V-007 单对象语义）
    expect(body.signals_json).toBeUndefined()
    // 成功后本地文档与乐观锁同步服务端权威稿
    expect(vm.detailEntry.signals_json).toEqual(updatedDoc)
    expect(vm.detailEntry.lock_version).toBe(4)
    wrapper.unmount()
  })

  it('缺少处置说明时不发起请求', async () => {
    const { wrapper, vm, fetchMock } = await mountView([pendingCandidate])
    vm.currentUser = 42
    vm.dismissRejectedCandidateId = 'sig_002'
    vm.dismissRejectedNote = '   '
    await vm.submitDismissRejectedCandidate()
    await flushPromises()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
    wrapper.unmount()
  })

  it('撤销处置确认后提交 revoke 单对象 PATCH', async () => {
    const { wrapper, vm, fetchMock } = await mountView(
      [dismissedCandidate],
      { signals_json: { schema_version: 2, signals: [], rejected_candidates: [pendingCandidate] }, lock_version: 4 },
    )

    const pending = vm.revokeRejectedDismissal(dismissedCandidate)
    await flushPromises()
    // ElMessageBox 确认框：点击「撤销处置」确认按钮
    const confirmButton = document.querySelector('.el-message-box__btns .el-button--primary') as HTMLElement | null
    expect(confirmButton).toBeTruthy()
    confirmButton!.click()
    await pending
    await flushPromises()

    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(patchCall).toBeTruthy()
    const body = JSON.parse(String(patchCall![1].body))
    expect(body.revoke_rejected_candidate_id).toBe('sig_003')
    expect(body.change_annotations).toEqual([
      { path: '/signals_json/rejected_candidates', reason_code: 'rejected_candidate_dismissal_revoked' },
    ])
    expect(body.signals_json).toBeUndefined()
    wrapper.unmount()
  })
})
