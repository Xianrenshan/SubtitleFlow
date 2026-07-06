/**
 * app.js - 前端业务核心 Vue 逻辑
 */
import { uploadDB } from './db.js';

const { ref, reactive, computed, onMounted, onUnmounted } = Vue;

const CHUNK_SIZE = 5 * 1024 * 1024;
const CHUNK_THRESHOLD = 5 * 1024 * 1024;

const app = Vue.createApp({
    setup() {
        const activeTab = ref('main');
        const fileList = ref([]);
        const selectedFile = ref(null);
        const uploading = ref(false);
        const task = ref(null);
        const elapsedSec = ref(0);
        const etaSec = ref(null);
        let pollingTimer = null;
        let elapsedTimer = null;
        let stepStartTime = null;
        let isUploadingFlag = false;
        const uploadChunkProgress = reactive({ uploaded: 0, total: 0 });
        const pendingUploads = ref([]);
        // 高级选项与字幕上传状态
        const advancedCollapsed = ref(false);
        const subtitleFile = ref(null);
        const subtitleFileList = ref([]);
        const skippedWhisper = ref(false);

        const steps = reactive([
            { label: '格式转换', active: false, completed: false },
            { label: '生成提示词', active: false, completed: false },
            { label: '语音识别', active: false, completed: false },
            { label: '分析与翻译', active: false, completed: false },
            { label: '字幕压制', active: false, completed: false },
        ]);

        const onFileChange = (file) => { selectedFile.value = file.raw; fileList.value = [file]; };
        const onSubtitleChange = (file) => { subtitleFile.value = file.raw; subtitleFileList.value = [file]; };

        /* ========== 上传逻辑 ========== */
        const uploadVideo = async () => {
            if (!selectedFile.value) return;
            uploading.value = true;
            isUploadingFlag = true;
            uploadChunkProgress.uploaded = 0;
            uploadChunkProgress.total = 0;
            try {
                const file = selectedFile.value;
                if (file.size >= CHUNK_THRESHOLD) { await chunkedUpload(file); } else { await simpleUpload(file); }
            } catch (e) { ElementPlus.ElMessage.error('上传失败: ' + e.message); }
            finally { uploading.value = false; isUploadingFlag = false; uploadChunkProgress.uploaded = 0; uploadChunkProgress.total = 0; }
        };

        const simpleUpload = async (file) => {
            const formData = new FormData();
            formData.append('file', file);
            if (subtitleFile.value) { formData.append('subtitle', subtitleFile.value); }
            const res = await fetch('/api/upload', { method: 'POST', body: formData });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '上传失败');
            task.value = { task_id: data.task_id, status: 'processing', step_progress: 0, current_step: '准备开始' };
            startPolling(data.task_id);
        };

        const chunkedUpload = async (file) => {
            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
            let uploadId = null;
            let startChunk = 0;
            const allPending = await uploadDB.getAll();
            const matching = allPending.find(p => p.fileName === file.name && p.fileSize === file.size);
            if (matching) {
                try {
                    const statusRes = await fetch('/api/upload/status/' + matching.uploadId);
                    if (statusRes.ok) {
                        const statusData = await statusRes.json();
                        if (statusData.status === 'uploading') {
                            uploadId = matching.uploadId;
                            const uploaded = statusData.uploaded_chunks || [];
                            startChunk = uploaded.length > 0 ? Math.max(...uploaded) + 1 : 0;
                            if (startChunk >= totalChunks) startChunk = totalChunks;
                            console.log('[upload] 续传: 从分片 ' + startChunk + '/' + totalChunks + ' 开始');
                        }
                    }
                } catch (e) { }
            }
            if (!uploadId) {
                const initRes = await fetch('/api/upload/init', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: file.name, file_size: file.size, total_chunks: totalChunks }) });
                const initData = await initRes.json();
                if (!initRes.ok) throw new Error(initData.detail || '初始化上传失败');
                uploadId = initData.upload_id;
                startChunk = 0;
            }
            await uploadDB.save({ uploadId, fileName: file.name, fileSize: file.size, totalChunks, timestamp: Date.now() });
            uploadChunkProgress.total = totalChunks;
            uploadChunkProgress.uploaded = startChunk;
            for (let i = startChunk; i < totalChunks; i++) {
                const start = i * CHUNK_SIZE;
                const end = Math.min(start + CHUNK_SIZE, file.size);
                const chunkBlob = file.slice(start, end);
                const formData = new FormData();
                formData.append('chunk', chunkBlob);
                const chunkRes = await fetch('/api/upload/chunk?upload_id=' + uploadId + '&chunk_index=' + i, { method: 'POST', body: formData });
                if (!chunkRes.ok) { const errData = await chunkRes.json().catch(() => ({})); throw new Error(errData.detail || '分片 ' + i + ' 上传失败'); }
                uploadChunkProgress.uploaded = i + 1;
                await uploadDB.save({ uploadId, fileName: file.name, fileSize: file.size, totalChunks, timestamp: Date.now() });
            }
            const hasSub = !!subtitleFile.value;
            const completeRes = await fetch('/api/upload/complete?upload_id=' + uploadId + '&has_subtitle=' + hasSub, { method: 'POST' });
            const completeData = await completeRes.json();
            if (!completeRes.ok) throw new Error(completeData.detail || '完成上传失败');
            await uploadDB.remove(uploadId);
            if (hasSub) {
                const subFormData = new FormData();
                subFormData.append('subtitle', subtitleFile.value);
                const subRes = await fetch('/api/upload/subtitle?task_id=' + completeData.task_id, { method: 'POST', body: subFormData });
                if (!subRes.ok) { const errSub = await subRes.json().catch(() => ({})); throw new Error(errSub.detail || '字幕上传失败'); }
            }
            task.value = { task_id: completeData.task_id, status: 'processing', step_progress: 0, current_step: '准备开始' };
            startPolling(completeData.task_id);
            await loadPendingUploads();
        };

        const cancelPendingUpload = async (uploadId) => { await uploadDB.remove(uploadId); await loadPendingUploads(); };
        const loadPendingUploads = async () => { const all = await uploadDB.getAll(); pendingUploads.value = all.map(p => ({ uploadId: p.uploadId, fileName: p.fileName, fileSize: p.fileSize })); };

        /* ========== 轮询 ========== */
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
                    if (t.status === 'success' || t.status === 'failed') { stopPolling(); stopElapsedTimer(); fetchHistory(); }
                } catch (e) { console.error('轮询出错', e); }
            }, 1500);
        };
        const stopPolling = () => { if (pollingTimer) clearInterval(pollingTimer); };
        const stopElapsedTimer = () => { if (elapsedTimer) clearInterval(elapsedTimer); };
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
                steps.forEach((s, i) => { s.active = (i === idx); s.completed = (i < idx); });
            }
            if (!current || current === '完成' || current === '全部完成') { steps.forEach(s => { s.active = false; s.completed = true; }); }
        };
        const startElapsedTimer = (startTs) => {
            stopElapsedTimer();
            elapsedTimer = setInterval(() => { elapsedSec.value = (Date.now() - startTs) / 1000; }, 200);
        };

        /* ========== 工具函数 ========== */
        const formatTime = (sec) => { if (sec == null || isNaN(sec)) return '--:--'; const m = Math.floor(sec / 60); const s = Math.floor(sec % 60); return m.toString().padStart(2, '0') + ':' + s.toString().padStart(2, '0'); };
        const formatFileSize = (bytes) => { if (!bytes) return '0 B'; const k = 1024; const sizes = ['B', 'KB', 'MB', 'GB']; const i = Math.floor(Math.log(bytes) / Math.log(k)); return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]; };
        const formatDate = (dateStr) => { if (!dateStr) return '-'; const normalized = dateStr.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(dateStr) ? dateStr : dateStr + 'Z'; const date = new Date(normalized); if (isNaN(date.getTime())) return '-'; return date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }); };
        const resetMain = () => { task.value = null; fileList.value = []; selectedFile.value = null; elapsedSec.value = 0; etaSec.value = null; skippedWhisper.value = false; advancedCollapsed.value = false; subtitleFile.value = null; subtitleFileList.value = []; steps.forEach(s => { s.active = false; s.completed = false; }); };

        /* ========== 历史记录 ========== */
        const historyTasks = ref([]);
        const historyFilter = reactive({ status: '', search: '' });
        let debounceTimer = null;
        const onSearchInput = () => { if (debounceTimer) clearTimeout(debounceTimer); debounceTimer = setTimeout(() => { fetchHistory(); }, 500); };
        const filteredHistoryTasks = computed(() => { return historyTasks.value; });
        const fetchHistory = async () => {
            try {
                const params = new URLSearchParams();
                if (historyFilter.status) params.append('status', historyFilter.status);
                if (historyFilter.search) params.append('search', historyFilter.search);
                params.append('page', '1'); params.append('page_size', '1000');
                const res = await fetch('/api/tasks?' + params.toString());
                const data = await res.json();
                if (res.ok) { historyTasks.value = (data.tasks || []).map(item => ({ ...item, showCrops: false, cropsLoaded: false, crops: [] })); }
            } catch (e) { ElementPlus.ElMessage.error('获取历史失败: ' + e.message); }
        };
        const deleteTask = async (taskId) => { try { const res = await fetch('/api/tasks/' + taskId, { method: 'DELETE' }); if (res.ok) { fetchHistory(); ElementPlus.ElMessage.success('删除成功'); } else throw new Error('删除失败'); } catch (e) { ElementPlus.ElMessage.error('删除失败: ' + e.message); } };
        const cleanupCompleted = async () => { try { const res = await fetch('/api/tasks/cleanup', { method: 'POST' }); if (res.ok) { fetchHistory(); ElementPlus.ElMessage.success('清理完成'); } else throw new Error('清理失败'); } catch (e) { ElementPlus.ElMessage.error('清理失败: ' + e.message); } };
        const downloadHistoryFile = (taskId, type) => { window.open('/api/download/' + taskId + '/' + type, '_blank'); };
        const toggleCrops = async (item) => {
            item.showCrops = !item.showCrops;
            if (item.showCrops && !item.cropsLoaded) {
                try {
                    const res = await fetch('/api/tasks/' + item.task_id + '/crops');
                    const data = await res.json();
                    if (res.ok) { item.crops = data.crops || []; item.cropsLoaded = true; }
                } catch (e) { ElementPlus.ElMessage.error('获取裁剪记录失败: ' + e.message); }
            }
        };
        const downloadCropFile = (cropId) => { window.open('/api/download/crop/' + cropId, '_blank'); };
        const reprocessTask = async (taskId) => { try { const res = await fetch('/api/tasks/' + taskId + '/reprocess', { method: 'POST' }); const data = await res.json(); if (!res.ok) throw new Error(data.detail || '重新制作失败'); steps.forEach(s => { s.active = false; s.completed = false; }); task.value = { task_id: taskId, status: 'processing', step_progress: 0, current_step: '准备开始' }; elapsedSec.value = 0; etaSec.value = null; selectedFile.value = null; fileList.value = []; activeTab.value = 'main'; startPolling(taskId); ElementPlus.ElMessage.success('已开始重新制作'); } catch (e) { ElementPlus.ElMessage.error('重新制作失败: ' + e.message); } };

        /* ========== 裁剪 ========== */
        const cropDialogVisible = ref(false);
        const currentCropTaskId = ref('');
        const cropSegments = ref([{ start: '00:03:04', end: '00:04:08' }]);
        const cropSubmitting = ref(false);
        const cropMode = ref('remove');
        const openCropDialog = (taskId) => { currentCropTaskId.value = taskId; cropSegments.value = [{ start: '00:03:04', end: '00:04:08' }]; cropMode.value = 'remove'; cropDialogVisible.value = true; };
        const addCropSegment = () => { cropSegments.value.push({ start: '', end: '' }); };
        const removeCropSegment = (idx) => { cropSegments.value.splice(idx, 1); };
        const submitCrop = async () => {
            if (!currentCropTaskId.value || cropSegments.value.some(seg => !seg.start || !seg.end)) { ElementPlus.ElMessage.warning('请填写完整的时间段'); return; }
            cropSubmitting.value = true;
            try {
                const res = await fetch('/api/tasks/' + currentCropTaskId.value + '/crop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segments: cropSegments.value, mode: cropMode.value }) });
                if (res.ok) { ElementPlus.ElMessage.success('裁剪任务已提交'); cropDialogVisible.value = false; fetchHistory(); startCropPolling(currentCropTaskId.value); } else { const err = await res.json(); throw new Error(err.detail || '提交失败'); }
            } catch (e) { ElementPlus.ElMessage.error('提交失败: ' + e.message); }
            finally { cropSubmitting.value = false; }
        };
        let cropPollTimer = null;
        const startCropPolling = (taskId) => {
            if (cropPollTimer) clearInterval(cropPollTimer);
            let lastCropCount = 0;
            cropPollTimer = setInterval(async () => {
                try {
                    const res = await fetch('/api/tasks/' + taskId + '/crops');
                    const data = await res.json();
                    if (!res.ok) return;
                    const crops = data.crops || [];
                    const allDone = crops.length > 0 && crops.every(c => c.status === 'success' || c.status === 'failed');
                    const item = historyTasks.value.find(t => t.task_id === taskId);
                    if (item && item.showCrops) { item.crops = crops; item.cropsLoaded = true; }
                    if (allDone) {
                        clearInterval(cropPollTimer);
                        cropPollTimer = null;
                        const successCount = crops.filter(c => c.status === 'success').length;
                        if (successCount > lastCropCount) ElementPlus.ElMessage.success(successCount + ' 个裁剪任务已完成');
                    }
                    lastCropCount = crops.filter(c => c.status === 'success').length;
                } catch (e) { console.error('裁剪轮询出错', e); }
            }, 2000);
        };

        /* ========== 配置 ========== */
        const configForm = reactive({
            features: { enable_asr_prompt: true, enable_ad_detection: false, enable_summary: false, enable_titles: false },
            ollama: { analysis: { model: 'qwen2:7b', temperature: 0.1 }, translate: { model: 'qwen2:7b', temperature: 0.1 } },
            translate_backend: 'ollama',
            online_api: { 
                provider: 'openai', 
                base_url: 'https://api.openai.com', api_key: '', model: 'gpt-3.5-turbo', batch_mode: false, batch_size: 5, fallbacks: [] 
            },
            local_translation: { model_dir: '', topic: '' },
            font: { zh: 'SimHei', en: 'Arial', scale: 1.0 }
        });

        const topicPreset = ref('');
        const configSaved = ref(false);

        // 厂商预设配置
        const PROVIDER_PRESETS = {
            'openai': { base_url: 'https://api.openai.com', model: 'gpt-3.5-turbo' },
            'deepseek': { base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
            'siliconflow': { base_url: 'https://api.siliconflow.cn', model: 'deepseek-ai/DeepSeek-V3' },
            'zhipu': { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4' },
            'qwen': { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-turbo' },
            'moonshot': { base_url: 'https://api.moonshot.cn', model: 'moonshot-v1-8k' },
        };

        // 主 API 厂商切换逻辑
        const onProviderChange = (val) => {
            const preset = PROVIDER_PRESETS[val];
            if (preset) {
                configForm.online_api.base_url = preset.base_url;
                configForm.online_api.model = preset.model;
                ElementPlus.ElMessage.success(`已切换至 ${val}，URL 与模型已更新`);
            }
        };

        // 备用 API 厂商切换逻辑
        const onFallbackProviderChange = (val, idx) => {
            const preset = PROVIDER_PRESETS[val];
            if (preset) {
                const fb = configForm.online_api.fallbacks[idx];
                if (fb) {
                    fb.base_url = preset.base_url;
                    fb.model = preset.model;
                    ElementPlus.ElMessage.success(`备用 ${idx + 1} 已切换至 ${val}，URL 与模型已更新`);
                }
            }
        };

        const onTopicChange = (val) => {
            const topics = { football: 'football match analysis tactics players', f1: 'F1 racing driver circuit lap time strategy', basketball: 'basketball game players tactics score', esports: 'esports game tournament players strategy' };
            if (val && val !== 'custom') configForm.local_translation.topic = topics[val];
            else if (val === 'custom') configForm.local_translation.topic = '';
            else configForm.local_translation.topic = '';
        };
        const addFallback = () => {
            if (!configForm.online_api.fallbacks) configForm.online_api.fallbacks = [];
            configForm.online_api.fallbacks.push({ name: '备用' + (configForm.online_api.fallbacks.length + 1), provider: 'openai', base_url: '', api_key: '', model: '' });
        };
        const removeFallback = (idx) => { configForm.online_api.fallbacks.splice(idx, 1); };
        const saveConfig = async () => {
            try {
                const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(configForm) });
                if (res.ok) { configSaved.value = true; ElementPlus.ElMessage.success('配置保存成功'); setTimeout(() => { configSaved.value = false; }, 2000); } else throw new Error('保存失败');
            } catch (e) { ElementPlus.ElMessage.error('保存失败: ' + e.message); }
        };
        const onBeforeUnload = (e) => { if (isUploadingFlag) { e.preventDefault(); e.returnValue = '视频正在上传中，离开可能导致上传中断。是否继续？'; } };
        
        onMounted(async () => {
            await uploadDB.init();
            await loadPendingUploads();
            fetchHistory();
            fetch('/api/config').then(res => res.json()).then(data => { Object.assign(configForm, data); if (!configForm.online_api.fallbacks) configForm.online_api.fallbacks = []; }).catch(() => {});
            window.addEventListener('beforeunload', onBeforeUnload);
        });
        onUnmounted(() => { stopPolling(); stopElapsedTimer(); if (cropPollTimer) clearInterval(cropPollTimer); window.removeEventListener('beforeunload', onBeforeUnload); });

        return { activeTab, fileList, selectedFile, uploading, task, elapsedSec, etaSec, steps, onFileChange, uploadVideo, resetMain, formatTime, formatFileSize, uploadChunkProgress, pendingUploads, cancelPendingUpload, historyFilter, filteredHistoryTasks, fetchHistory, deleteTask, cleanupCompleted, downloadHistoryFile, toggleCrops, downloadCropFile, reprocessTask, cropDialogVisible, currentCropTaskId, cropSegments, cropSubmitting, cropMode, openCropDialog, addCropSegment, removeCropSegment, submitCrop, configForm, topicPreset, configSaved, onTopicChange, saveConfig, addFallback, removeFallback, formatDate, onSearchInput, advancedCollapsed, subtitleFile, subtitleFileList, onSubtitleChange, skippedWhisper, onProviderChange, onFallbackProviderChange };
    }
});
app.use(ElementPlus);
app.mount('#app');