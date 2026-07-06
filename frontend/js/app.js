/**
 * app.js - 核心整合入口文件，组配所有的 Composable 模块
 */
import { uploadDB } from './db.js';
import { formatTime, formatFileSize, formatDate } from './utils.js';
import { useConfig } from './modules/useConfig.js';
import { useUpload } from './modules/useUpload.js';
import { useHistory } from './modules/useHistory.js';
import { useCrop } from './modules/useCrop.js';

const { ref, reactive, onMounted, onUnmounted } = window.Vue;

const app = window.Vue.createApp({
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

        /* ========== 任务全局时间及状态轮询核心 ========== */
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
            
            // 重置子模块中的上传部分状态
            fileList.value = [];
            selectedFile.value = null;
            advancedCollapsed.value = false;
            subtitleFile.value = null;
            subtitleFileList.value = [];
            
            steps.forEach(s => { s.active = false; s.completed = false; }); 
        };

        /* ========== 模块实例化与装配 ========== */
        const configModule = useConfig();
        const uploadModule = useUpload(task, startPolling);
        const historyModule = useHistory(task, steps, startPolling, resetMain, activeTab);
        const cropModule = useCrop(historyModule.fetchHistory, historyModule.historyTasks);

        // 统一解构出需要被 resetMain 主动写回更新的上传模块属性
        const { fileList, selectedFile, advancedCollapsed, subtitleFile, subtitleFileList } = uploadModule;
        const { fetchHistory } = historyModule;

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
    }
});

app.use(window.ElementPlus);
app.mount('#app');