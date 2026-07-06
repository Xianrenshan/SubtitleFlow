/**
 * useCrop.js - 视频切片裁剪业务 Composable
 */
const { ref } = window.Vue;

export function useCrop(fetchHistory, historyTasks) {
    const cropDialogVisible = ref(false);
    const currentCropTaskId = ref('');
    const cropSegments = ref([{ start: '00:03:04', end: '00:04:08' }]);
    const cropSubmitting = ref(false);
    const cropMode = ref('remove');

    const openCropDialog = (taskId) => { 
        currentCropTaskId.value = taskId; 
        cropSegments.value = [{ start: '00:03:04', end: '00:04:08' }]; 
        cropMode.value = 'remove'; 
        cropDialogVisible.value = true; 
    };

    const addCropSegment = () => { 
        cropSegments.value.push({ start: '', end: '' }); 
    };

    const removeCropSegment = (idx) => { 
        cropSegments.value.splice(idx, 1); 
    };

    const submitCrop = async () => {
        if (!currentCropTaskId.value || cropSegments.value.some(seg => !seg.start || !seg.end)) { 
            window.ElementPlus.ElMessage.warning('请填写完整的时间段'); 
            return; 
        }
        cropSubmitting.value = true;
        try {
            const res = await fetch('/api/tasks/' + currentCropTaskId.value + '/crop', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json' }, 
                body: JSON.stringify({ segments: cropSegments.value, mode: cropMode.value }) 
            });
            if (res.ok) { 
                window.ElementPlus.ElMessage.success('裁剪任务已提交'); 
                cropDialogVisible.value = false; 
                fetchHistory(); 
                startCropPolling(currentCropTaskId.value); 
            } else { 
                const err = await res.json(); 
                throw new Error(err.detail || '提交失败'); 
            }
        } catch (e) { 
            window.ElementPlus.ElMessage.error('提交失败: ' + e.message); 
        } finally { 
            cropSubmitting.value = false; 
        }
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
                
                if (item && item.showCrops) { 
                    item.crops = crops; 
                    item.cropsLoaded = true; 
                }
                
                if (allDone) {
                    clearInterval(cropPollTimer);
                    cropPollTimer = null;
                    const successCount = crops.filter(c => c.status === 'success').length;
                    if (successCount > lastCropCount) {
                        window.ElementPlus.ElMessage.success(successCount + ' 个裁剪任务已完成');
                    }
                }
                lastCropCount = crops.filter(c => c.status === 'success').length;
            } catch (e) { 
                console.error('裁剪轮询出错', e); 
            }
        }, 2000);
    };

    const clearCropPolling = () => {
        if (cropPollTimer) {
            clearInterval(cropPollTimer);
            cropPollTimer = null;
        }
    };

    return {
        cropDialogVisible,
        currentCropTaskId,
        cropSegments,
        cropSubmitting,
        cropMode,
        openCropDialog,
        addCropSegment,
        removeCropSegment,
        submitCrop,
        clearCropPolling
    };
}