/**
 * app.js - 核心整合入口文件，注册异步模板组件，实现状态分发
 */
import { uploadDB } from './db.js';
import { formatTime, formatFileSize, formatDate } from './utils.js';
import { useConfig } from './modules/useConfig.js';
import { useUpload } from './modules/useUpload.js';
import { useHistory } from './modules/useHistory.js';
import { useCrop } from './modules/useCrop.js';

const { ref, reactive, onMounted, onUnmounted, defineAsyncComponent, provide, inject } = window.Vue;

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

/* ==================== 2. 主 App 创建与挂载 ==================== */

const app = window.Vue.createApp({
    components: {
        UploadTab,
        HistoryTab,
        ConfigTab,
        CropDialog
    },
    setup() {
        const activeTab = ref('main');
        const task = ref(null);
        const elapsedSec = ref(0);
        const etaSec = ref(null);
        const skippedWhisper = ref(false);

        let pollingTimer = null;
        let elapsedTimer = null;
        let stepStartTime = null;

        const steps = reactive([
            { label: '格式转换', active: false, completed: false },
            { label: '生成提示词', active: false, completed: false },
            { label: '语音识别', active: false, completed: false },
            { label: '分析与翻译', active: false, completed: false },
            { label: '字幕压制', active: false, completed: false },
        ]);

        /* ========== 任务全局状态轮询 ========== */
        const startPolling = (taskId) => {
            stopPolling();
            pollingTimer = setInterval(async () => {
                try {
                    const res = await fetch('/api/task/' + taskId);
                    const t = await res.json();
                    if (!res.ok) throw new Error(t.detail);
                    task.value = t;
                    updateSteps(t.current_step);
                    if (t.step_started_at) {
                        stepStartTime = new Date(t.step_started_at + 'Z').getTime();
                        startElapsedTimer(stepStartTime);
                    }
                    etaSec.value = t.eta_sec ?? null;
                    if (t.status === 'success' || t.status === 'failed') { 
                        stopPolling(); 
                        stopElapsedTimer(); 
                        fetchHistory(); 
                    }
                } catch (e) { 
                    console.error('轮询出错', e); 
                }
            }, 1500);
        };

        const stopPolling = () => { 
            if (pollingTimer) clearInterval(pollingTimer); 
        };

        const stopElapsedTimer = () => { 
            if (elapsedTimer) clearInterval(elapsedTimer); 
        };

        const updateSteps = (current) => {
            const map = { '格式转换': 0, '生成ASR提示词': 1, '语音识别': 2, '分析与翻译': 3, '压制字幕': 4 };
            const idx = map[current];
            if (current === '分析与翻译' && !steps[2].completed) {
                skippedWhisper.value = true;
                steps[0].completed = true;
                steps[1].completed = true;
                steps[2].active = false;
                steps[2].completed = true;
                steps[3].active = true;
            } else {
                skippedWhisper.value = false;
                steps.forEach((s, i) => { 
                    s.active = (i === idx); 
                    s.completed = (i < idx); 
                });
            }
            if (!current || current === '完成' || current === '全部完成') { 
                steps.forEach(s => { s.active = false; s.completed = true; }); 
            }
        };

        const startElapsedTimer = (startTs) => {
            stopElapsedTimer();
            elapsedTimer = setInterval(() => { 
                elapsedSec.value = (Date.now() - startTs) / 1000; 
            }, 200);
        };

        const resetMain = () => { 
            task.value = null; 
            elapsedSec.value = 0; 
            etaSec.value = null; 
            skippedWhisper.value = false; 
            
            // 响应修改子模块中的上传状态
            fileList.value = [];
            selectedFile.value = null;
            advancedCollapsed.value = false;
            subtitleFile.value = null;
            subtitleFileList.value = [];
            
            steps.forEach(s => { s.active = false; s.completed = false; }); 
        };

        /* ========== 组合式业务逻辑装配 ========== */
        const configModule = useConfig();
        const uploadModule = useUpload(task, startPolling);
        const historyModule = useHistory(task, steps, startPolling, resetMain, activeTab);
        const cropModule = useCrop(historyModule.fetchHistory, historyModule.historyTasks);

        const { fileList, selectedFile, advancedCollapsed, subtitleFile, subtitleFileList } = uploadModule;
        const { fetchHistory } = historyModule;

        // 全局依赖注入：拼装所有需要被组件模板访问的数据与接口
        const rootState = {
            activeTab,
            task,
            elapsedSec,
            etaSec,
            steps,
            skippedWhisper,
            resetMain,
            formatTime,
            formatFileSize,
            formatDate,
            ...configModule,
            ...uploadModule,
            ...historyModule,
            ...cropModule
        };

        provide('store', rootState);

        onMounted(async () => {
            await uploadDB.init();
            await uploadModule.loadPendingUploads();
            fetchHistory();
            configModule.loadConfig();
            window.addEventListener('beforeunload', uploadModule.handleBeforeUnload);
        });

        onUnmounted(() => { 
            stopPolling(); 
            stopElapsedTimer(); 
            cropModule.clearCropPolling(); 
            window.removeEventListener('beforeunload', uploadModule.handleBeforeUnload); 
        });

        return {
            activeTab
        };
    }
});

app.use(window.ElementPlus);
app.mount('#app');