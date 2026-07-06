/**
 * useUpload.js - 视频与字幕文件上传 Composable
 */
import { uploadDB } from '../db.js';

const { ref, reactive } = window.Vue;

const CHUNK_SIZE = 5 * 1024 * 1024;
const CHUNK_THRESHOLD = 5 * 1024 * 1024;

export function useUpload(task, startPolling) {
    const fileList = ref([]);
    const selectedFile = ref(null);
    const uploading = ref(false);
    const uploadChunkProgress = reactive({ uploaded: 0, total: 0 });
    const pendingUploads = ref([]);
    const advancedCollapsed = ref(false);
    const subtitleFile = ref(null);
    const subtitleFileList = ref([]);

    let isUploadingFlag = false;

    const onFileChange = (file) => { 
        selectedFile.value = file.raw; 
        fileList.value = [file]; 
    };
    
    const onSubtitleChange = (file) => { 
        subtitleFile.value = file.raw; 
        subtitleFileList.value = [file]; 
    };

    const uploadVideo = async () => {
        if (!selectedFile.value) return;
        uploading.value = true;
        isUploadingFlag = true;
        uploadChunkProgress.uploaded = 0;
        uploadChunkProgress.total = 0;
        try {
            const file = selectedFile.value;
            if (file.size >= CHUNK_THRESHOLD) { 
                await chunkedUpload(file); 
            } else { 
                await simpleUpload(file); 
            }
        } catch (e) { 
            window.ElementPlus.ElMessage.error('上传失败: ' + e.message); 
        } finally { 
            uploading.value = false; 
            isUploadingFlag = false; 
            uploadChunkProgress.uploaded = 0; 
            uploadChunkProgress.total = 0; 
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
            const initRes = await fetch('/api/upload/init', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json' }, 
                body: JSON.stringify({ filename: file.name, file_size: file.size, total_chunks: totalChunks }) 
            });
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
            if (!chunkRes.ok) { 
                const errData = await chunkRes.json().catch(() => ({})); 
                throw new Error(errData.detail || '分片 ' + i + ' 上传失败'); 
            }
            
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
            if (!subRes.ok) { 
                const errSub = await subRes.json().catch(() => ({})); 
                throw new Error(errSub.detail || '字幕上传失败'); 
            }
        }
        
        task.value = { task_id: completeData.task_id, status: 'processing', step_progress: 0, current_step: '准备开始' };
        startPolling(completeData.task_id);
        await loadPendingUploads();
    };

    const cancelPendingUpload = async (uploadId) => { 
        await uploadDB.remove(uploadId); 
        await loadPendingUploads(); 
    };

    const loadPendingUploads = async () => { 
        const all = await uploadDB.getAll(); 
        pendingUploads.value = all.map(p => ({ uploadId: p.uploadId, fileName: p.fileName, fileSize: p.fileSize })); 
    };

    const handleBeforeUnload = (e) => { 
        if (isUploadingFlag) { 
            e.preventDefault(); 
            e.returnValue = '视频正在上传中，离开可能导致上传中断。是否继续？'; 
        } 
    };

    return {
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
        handleBeforeUnload
    };
}