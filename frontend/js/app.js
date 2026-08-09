/**
 * app.js - 核心整合入口文件（上传与计算分离版）
 *
 * 核心变更：
 * - 从单任务追踪改为多任务队列管理
 * - 新增 taskQueue（队列列表）+ focusTaskId（聚焦任务）
 * - startPolling 只轮询聚焦任务详情
 * - 新增 startQueuePolling 轮询整个队列
 * - 新增 enqueueSelected / pauseTask / stopTask / requeueTask
 */
import { uploadDB } from './db.js';
import { formatTime, formatFileSize, formatDate } from './utils.js';
import { useConfig } from './modules/useConfig.js';
import { useUpload } from './modules/useUpload.js';
import { useHistory } from './modules/useHistory.js';
import { useCrop } from './modules/useCrop.js';

const { ref, reactive, onMounted, onUnmounted, defineAsyncComponent, provide, inject, computed } = window.Vue;

/* ==================== 1. 动态读取外部 HTML 的组件声明 ==================== */

const UploadTab = defineAsyncComponent(async () => {
    const html = await fetch('./templates/upload-tab.html').then(r => r.text());
    return {
        template: html,
        setup() {
            const store = inject('store');
            return { ...store };
        }
    };
});

const HistoryTab = defineAsyncComponent(async () => {
    const html = await fetch('./templates/history-tab.html').then(r => r.text());
    return {
        template: html,
        setup() {
            const store = inject('store');
            return { ...store };
        }
    };
});

const ConfigTab = defineAsyncComponent(async () => {
    const html = await fetch('./templates/config-tab.html').then(r => r.text());
    return {
        template: html,
        setup() {
            const store = inject('store');
            return { ...store };
        }
    };
});

const CropDialog = defineAsyncComponent(async () => {
    const html = await fetch('./templates/crop-dialog.html').then(r => r.text());
    return {
        template: html,
        setup() {
            const store = inject('store');
            return { ...store };
        }
    };
});

/* ==================== 2. 主应用 ==================== */

const app = window.Vue.createApp({
    components: { UploadTab, HistoryTab, ConfigTab, CropDialog },
    setup() {
        const activeTab = ref('main');

        // ===== 聚焦任务（详情面板）=====
        const task = ref(null);
        const focusTaskId = ref(null);
        const steps = reactive([
            { title: '语音识别', desc: 'Whisper 转录', active: false, completed: false },
            { title: '字幕翻译', desc: '英译中', active: false, completed: false },
            { title: '字幕压制', desc: 'FFmpeg 烧录', active: false, completed: false },
        ]);

        // ===== 任务队列 =====
        const taskQueue = ref([]);

        // ===== 计时器 =====
        const elapsedSec = ref(0);
        const etaSec = ref(0);
        const skippedWhisper = ref(false);

        let pollingTimer = null;
        let queuePollingTimer = null;
        let elapsedTimer = null;
        let stepStartTime = null;

        // ==================== 状态辅助函数 ====================

        const statusTagType = (status) => {
            const map = {
                uploaded: 'info',
                waiting: 'info',
                processing: 'warning',
                success: 'success',
                failed: 'danger',
                paused: 'info',
                interrupted: 'danger',
            };
            return map[status] || 'info';
        };

        const statusLabel = (status) => {
            const map = {
                uploaded: '已上传',
                waiting: '等待中',
                processing: '处理中',
                success: '成功',
                failed: '失败',
                paused: '已暂停',
                interrupted: '已中断',
            };
            return map[status] || status;
        };

        // ==================== 队列轮询 ====================

        const fetchTaskQueue = async () => {
            try {
                const res = await fetch('/api/tasks?status=waiting,processing,paused,interrupted&page=1&page_size=200');
                const data = await res.json();
                if (res.ok) {
                    taskQueue.value = (data.tasks || []).map(t => ({
                        task_id: t.task_id,
                        status: t.status,
                        progress: t.progress || 0,
                        current_step: t.current_step || '',
                        original_filename: t.original_filename || '',
                        file_size: t.file_size,
                        created_at: t.created_at,
                        updated_at: t.updated_at,
                        error_message: t.error_message,
                    }));
                }
            } catch (e) {
                console.error('获取任务队列失败:', e);
            }
        };

        const startQueuePolling = () => {
            stopQueuePolling();
            queuePollingTimer = setInterval(async () => {
                await fetchTaskQueue();
                // 同时轮询聚焦任务详情
                if (focusTaskId.value) {
                    await pollFocusTask();
                }
            }, 1500);
        };

        const stopQueuePolling = () => {
            if (queuePollingTimer) {
                clearInterval(queuePollingTimer);
                queuePollingTimer = null;
            }
        };

        // ==================== 聚焦任务轮询（详情面板）====================

        const startPolling = (taskId) => {
            stopPolling();
            focusTaskId.value = taskId;
            pollingTimer = setInterval(async () => {
                await pollFocusTask();
            }, 1000);
            startElapsedTimer();
        };

        const stopPolling = () => {
            if (pollingTimer) {
                clearInterval(pollingTimer);
                pollingTimer = null;
            }
            stopElapsedTimer();
        };

        const pollFocusTask = async () => {
            if (!focusTaskId.value) return;
            try {
                const res = await fetch('/api/task/' + focusTaskId.value);
                const t = await res.json();
                if (!res.ok) return;

                task.value = t;

                // 更新步骤状态
                const stepMap = {
                    '语音识别': 0, '转录中': 0, 'Whisper': 0,
                    '翻译': 1, '英译中': 1, '字幕翻译': 1,
                    '压制': 2, '烧录': 2, 'FFmpeg': 2, '字幕压制': 2,
                };
                steps.forEach(s => { s.active = false; s.completed = false; });
                if (t.current_step) {
                    for (const [keyword, idx] of Object.entries(stepMap)) {
                        if (t.current_step.includes(keyword)) {
                            for (let i = 0; i < idx; i++) steps[i].completed = true;
                            steps[idx].active = true;
                            break;
                        }
                    }
                }

                // 计时
                if (t.step_started_at) {
                    stepStartTime = new Date(t.step_started_at.endsWith('Z') ? t.step_started_at : t.step_started_at + 'Z');
                }
                if (t.eta_sec !== null && t.eta_sec !== undefined) {
                    etaSec.value = t.eta_sec;
                }

                // 跳过 Whisper
                if (t.current_step && t.current_step.includes('跳过')) {
                    skippedWhisper.value = true;
                }

                // 任务结束
                if (t.status === 'success' || t.status === 'failed' || t.status === 'interrupted') {
                    steps.forEach(s => {
                        if (t.status === 'success') { s.active = false; s.completed = true; }
                    });
                    stopPolling();
                    // 刷新队列和历史
                    fetchTaskQueue();
                    if (historyModule) historyModule.fetchHistory();
                }
            } catch (e) {
                console.error('轮询任务详情失败:', e);
            }
        };

        // ==================== 计时器 ====================

        const startElapsedTimer = () => {
            stopElapsedTimer();
            elapsedTimer = setInterval(() => {
                if (stepStartTime) {
                    elapsedSec.value = Math.floor((Date.now() - stepStartTime.getTime()) / 1000);
                }
            }, 1000);
        };

        const stopElapsedTimer = () => {
            if (elapsedTimer) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
        };

        // ==================== 聚焦任务 ====================

        const focusTask = (taskId) => {
            // 如果点击的是当前聚焦的任务，不重复
            if (focusTaskId.value === taskId && task.value) return;

            // 查找队列中的任务信息
            const queueTask = taskQueue.value.find(t => t.task_id === taskId);
            if (queueTask) {
                task.value = {
                    task_id: queueTask.task_id,
                    status: queueTask.status,
                    current_step: queueTask.current_step || '',
                    step_progress: queueTask.progress || 0,
                    progress: queueTask.progress || 0,
                    original_filename: queueTask.original_filename,
                };

                // 如果是处理中或等待中，开始轮询详情
                if (queueTask.status === 'processing' || queueTask.status === 'waiting') {
                    startPolling(taskId);
                } else {
                    // 已完成/失败/暂停/中断，直接拉取详情
                    focusTaskId.value = taskId;
                    pollFocusTask();
                }
            }
        };

        // ==================== 队列操作 ====================

        const enqueueSelected = async () => {
            const ids = [...uploadModule.selectedPendingIds.value];
            if (ids.length === 0) {
                window.ElementPlus.ElMessage.warning('请先选择文件');
                return;
            }
            try {
                const res = await fetch('/api/tasks/batch-enqueue', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_ids: ids }),
                });
                const data = await res.json();
                if (res.ok) {
                    const successCount = (data.results || []).filter(r => r.success).length;
                    window.ElementPlus.ElMessage.success(successCount + ' 个任务已加入队列');
                    uploadModule.selectedPendingIds.value = [];
                    uploadModule.selectAllPending.value = false;
                    await uploadModule.fetchPendingFiles();
                    await fetchTaskQueue();
                    startQueuePolling();
                } else {
                    window.ElementPlus.ElMessage.error(data.detail || '入队失败');
                }
            } catch (e) {
                window.ElementPlus.ElMessage.error('入队失败: ' + e.message);
            }
        };

        const enqueueAll = async () => {
            const ids = uploadModule.pendingFiles.value.map(f => f.task_id);
            if (ids.length === 0) {
                window.ElementPlus.ElMessage.warning('暂存区为空');
                return;
            }
            uploadModule.selectedPendingIds.value = ids;
            await enqueueSelected();
        };

        const pauseTask = async (taskId) => {
            try {
                const res = await fetch('/api/tasks/' + taskId + '/pause', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    window.ElementPlus.ElMessage.success('已暂停');
                    await fetchTaskQueue();
                } else {
                    window.ElementPlus.ElMessage.error(data.detail || '暂停失败');
                }
            } catch (e) {
                window.ElementPlus.ElMessage.error('暂停失败: ' + e.message);
            }
        };

        const stopTask = async (taskId) => {
            try {
                const res = await fetch('/api/tasks/' + taskId + '/stop', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    window.ElementPlus.ElMessage.success('已停止');
                    await fetchTaskQueue();
                } else {
                    window.ElementPlus.ElMessage.error(data.detail || '停止失败');
                }
            } catch (e) {
                window.ElementPlus.ElMessage.error('停止失败: ' + e.message);
            }
        };

        const requeueTask = async (taskId) => {
            try {
                const res = await fetch('/api/tasks/' + taskId + '/requeue', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    window.ElementPlus.ElMessage.success('已重新入队');
                    await fetchTaskQueue();
                    startQueuePolling();
                } else {
                    window.ElementPlus.ElMessage.error(data.detail || '重新入队失败');
                }
            } catch (e) {
                window.ElementPlus.ElMessage.error('重新入队失败: ' + e.message);
            }
        };

        // ==================== 重置 ====================

        const resetMain = () => {
            task.value = null;
            focusTaskId.value = null;
            stopPolling();
            steps.forEach(s => { s.active = false; s.completed = false; });
            elapsedSec.value = 0;
            etaSec.value = 0;
            skippedWhisper.value = false;
            stepStartTime = null;
        };

        // ==================== 模块装配 ====================

        const uploadModule = useUpload(fetchTaskQueue);
        const historyModule = useHistory(task, steps, startPolling, resetMain, activeTab, fetchTaskQueue);
        const configModule = useConfig();
        const cropModule = useCrop(historyModule.fetchHistory, historyModule.historyTasks);

        // ==================== 生命周期 ====================

        onMounted(async () => {
            await uploadDB.init();

            // 加载暂存区文件
            await uploadModule.fetchPendingFiles();
            // 加载任务队列
            await fetchTaskQueue();
            // 启动队列轮询（如果有活跃任务）
            if (taskQueue.value.length > 0) {
                startQueuePolling();
            }

            // 自动检测后台正在运行的任务
            try {
                const res = await fetch('/api/tasks?status=processing,waiting&page=1&page_size=10');
                const data = await res.json();
                if (res.ok && data.tasks && data.tasks.length > 0) {
                    // 聚焦第一个 processing 任务
                    const procTask = data.tasks.find(t => t.status === 'processing');
                    if (procTask) {
                        focusTask(procTask.task_id);
                    }
                    startQueuePolling();
                }
            } catch (e) {
                console.warn('自动检测后台任务失败:', e);
            }
        });

        onUnmounted(() => {
            stopPolling();
            stopQueuePolling();
            stopElapsedTimer();
            cropModule.clearCropPolling();
            window.removeEventListener('beforeunload', uploadModule.handleBeforeUnload);
        });

        // ==================== provide 给模板 ====================

        const store = {
            // 通用
            activeTab,
            task,
            focusTaskId,
            steps,
            taskQueue,
            elapsedSec,
            etaSec,
            skippedWhisper,
            // 辅助
            formatTime,
            formatFileSize,
            formatDate,
            statusTagType,
            statusLabel,
            // 队列操作
            fetchTaskQueue,
            startQueuePolling,
            stopQueuePolling,
            startPolling,
            stopPolling,
            focusTask,
            enqueueSelected,
            enqueueAll,
            pauseTask,
            stopTask,
            requeueTask,
            resetMain,
            // 上传模块
            ...uploadModule,
            // 历史模块
            ...historyModule,
            // 配置模块
            ...configModule,
            // 裁剪模块
            ...cropModule,
        };

        provide('store', store);

        return store;
    }
});

app.use(window.ElementPlus);
app.mount('#app');
