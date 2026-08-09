/**
 * useUpload.js - 视频与字幕文件上传 Composable（上传与计算分离版）
 *
 * 核心变更：
 * - 上传完成后不再启动流水线，只创建 uploaded 状态的暂存记录
 * - 新增 pendingFiles（暂存文件列表）、selectedPendingIds（选中项）
 * - 新增 fetchPendingFiles、deletePendingFile、clearPendingFiles
 * - 支持多文件批量上传
 */
import { uploadDB } from '../db.js';

const { ref, reactive, computed } = window.Vue;

const CHUNK_SIZE = 5 * 1024 * 1024;
const CHUNK_THRESHOLD = 5 * 1024 * 1024;

export function useUpload(fetchTaskQueue) {
    const fileList = ref([]);
    const selectedFile = ref(null);
    const uploading = ref(false);
    const uploadChunkProgress = reactive({
        uploaded: 0,
        total: 0,
        current: 0,   // 当前上传第几个文件
        totalFiles: 0,
    });
    const pendingUploads = ref([]);
    const advancedCollapsed = ref(false);
    const subtitleFile = ref(null);
    const subtitleFileList = ref([]);

    // 新增：暂存区文件列表（uploaded 状态的任务）
    const pendingFiles = ref([]);
    const selectedPendingIds = ref([]);
    const selectAllPending = ref(false);

    let isUploadingFlag = false;

    // ==================== 文件选择 ====================

    const onFileChange = (file) => {
        // el-upload multiple 模式：每次选文件都触发，收集到 fileList
        // fileList 由 el-upload v-model 管理，这里只需同步
        selectedFile.value = file.raw;
    };

    const onSubtitleChange = (file) => {
        subtitleFile.value = file.raw;
        subtitleFileList.value = [file];
    };

    // ==================== 批量上传 ====================

    const uploadVideo = async () => {
        if (fileList.value.length === 0) {
            window.ElementPlus.ElMessage.warning('请先选择文件');
            return;
        }
        uploading.value = true;
        isUploadingFlag = true;
        uploadChunkProgress.totalFiles = fileList.value.length;

        try {
            for (let i = 0; i < fileList.value.length; i++) {
                const file = fileList.value[i].raw;
                uploadChunkProgress.current = i + 1;
                uploadChunkProgress.uploaded = 0;
                uploadChunkProgress.total = 0;

                if (file.size >= CHUNK_THRESHOLD) {
                    await chunkedUpload(file);
                } else {
                    await simpleUpload(file);
                }
            }

            // 全部上传完成
            window.ElementPlus.ElMessage.success(fileList.value.length + ' 个文件上传完成');
            fileList.value = [];
            selectedFile.value = null;
            subtitleFile.value = null;
            subtitleFileList.value = [];

            // 刷新暂存区
            await fetchPendingFiles();
            // 刷新队列（如果有其他任务在跑）
            if (fetchTaskQueue) {
                await fetchTaskQueue();
            }
        } catch (e) {
            window.ElementPlus.ElMessage.error('上传失败: ' + e.message);
        } finally {
            uploading.value = false;
            isUploadingFlag = false;
            uploadChunkProgress.uploaded = 0;
            uploadChunkProgress.total = 0;
            uploadChunkProgress.current = 0;
            uploadChunkProgress.totalFiles = 0;
        }
    };

    const simpleUpload = async (file) => {
        const formData = new FormData();
        formData.append('file', file);
        if (subtitleFile.value) {
            formData.append('subtitle', subtitleFile.value);
        }

        const res = await fetch('/api/upload', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '上传失败');
        // 不再启动轮询，只返回
    };

    const chunkedUpload = async (file) => {
        const totalChunks = Math.ceil(file.size / CHUNK_SIZE);

        // 初始化分片上传会话
        const initRes = await fetch(
            `/api/upload/chunk/init?filename=${encodeURIComponent(file.name)}&file_size=${file.size}&total_chunks=${totalChunks}&has_subtitle=${!!subtitleFile.value}`,
            { method: 'POST' }
        );
        const initData = await initRes.json();
        if (!initRes.ok) throw new Error(initData.detail || '初始化上传失败');

        const uploadId = initData.upload_id;
        uploadChunkProgress.total = totalChunks;

        // 逐片上传
        for (let i = 0; i < totalChunks; i++) {
            const start = i * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, file.size);
            const chunk = file.slice(start, end);

            const chunkRes = await fetch(`/api/upload/chunk/${uploadId}/${i}`, {
                method: 'POST',
                body: (() => {
                    const fd = new FormData();
                    fd.append('chunk', chunk, `chunk_${i}`);
                    return fd;
                })(),
            });
            if (!chunkRes.ok) {
                const err = await chunkRes.json().catch(() => ({}));
                throw new Error(err.detail || `分片 ${i} 上传失败`);
            }
            uploadChunkProgress.uploaded = i + 1;
        }

        // 合并文件
        const completeFormData = new FormData();
        if (subtitleFile.value) {
            completeFormData.append('subtitle', subtitleFile.value);
        }
        const completeRes = await fetch(`/api/upload/chunk/${uploadId}/complete`, {
            method: 'POST',
            body: completeFormData,
        });
        const completeData = await completeRes.json();
        if (!completeRes.ok) throw new Error(completeData.detail || '文件合并失败');

        // 不再启动轮询
    };

    // ==================== 暂存区管理 ====================

    const fetchPendingFiles = async () => {
        try {
            const res = await fetch('/api/tasks?status=uploaded&page=1&page_size=1000');
            const data = await res.json();
            if (res.ok) {
                pendingFiles.value = data.tasks || [];
            }
        } catch (e) {
            console.error('获取待处理文件失败:', e);
        }
    };

    const deletePendingFile = async (taskId) => {
        try {
            const res = await fetch('/api/upload/pending/' + taskId, { method: 'DELETE' });
            if (res.ok) {
                // 从选中项中移除
                selectedPendingIds.value = selectedPendingIds.value.filter(id => id !== taskId);
                await fetchPendingFiles();
                window.ElementPlus.ElMessage.success('已删除');
            }
        } catch (e) {
            window.ElementPlus.ElMessage.error('删除失败: ' + e.message);
        }
    };

    const clearPendingFiles = async () => {
        try {
            const res = await fetch('/api/upload/pending', { method: 'DELETE' });
            if (res.ok) {
                selectedPendingIds.value = [];
                selectAllPending.value = false;
                await fetchPendingFiles();
                window.ElementPlus.ElMessage.success('已清空暂存区');
            }
        } catch (e) {
            window.ElementPlus.ElMessage.error('清空失败: ' + e.message);
        }
    };

    const toggleSelectAll = (val) => {
        if (val) {
            selectedPendingIds.value = pendingFiles.value.map(f => f.task_id);
        } else {
            selectedPendingIds.value = [];
        }
    };

    // ==================== 断点续传管理（IndexedDB） ====================

    const loadPendingUploads = async () => {
        await uploadDB.init();
        await loadPendingUploadsFromDB();
    };

    const cancelPendingUpload = async (uploadId) => {
        await uploadDB.remove(uploadId);
        await loadPendingUploads();
    };

    const loadPendingUploadsFromDB = async () => {
        const all = await uploadDB.getAll();
        pendingUploads.value = all.map(p => ({ uploadId: p.uploadId, fileName: p.fileName, fileSize: p.fileSize }));
    };

    // ==================== 离开警告 ====================

    const handleBeforeUnload = (e) => {
        if (isUploadingFlag) {
            e.preventDefault();
            e.returnValue = '视频正在上传中，离开可能导致上传中断。是否继续？';
        }
    };

    return {
        // 上传相关
        fileList,
        selectedFile,
        uploading,
        uploadChunkProgress,
        pendingUploads,
        advancedCollapsed,
        subtitleFile,
        subtitleFileList,
        onFileChange,
        onSubtitleChange,
        uploadVideo,
        cancelPendingUpload,
        loadPendingUploads,
        handleBeforeUnload,
        // 暂存区相关
        pendingFiles,
        selectedPendingIds,
        selectAllPending,
        fetchPendingFiles,
        deletePendingFile,
        clearPendingFiles,
        toggleSelectAll,
    };
}
