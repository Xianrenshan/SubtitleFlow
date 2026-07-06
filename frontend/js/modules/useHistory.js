/**
 * useHistory.js - 历史记录查询与重试管理 Composable
 */
const { ref, reactive, computed } = window.Vue;

export function useHistory(task, steps, startPolling, resetMain, activeTab) {
    const historyTasks = ref([]);
    const historyFilter = reactive({ status: '', search: '' });
    let debounceTimer = null;

    const onSearchInput = () => { 
        if (debounceTimer) clearTimeout(debounceTimer); 
        debounceTimer = setTimeout(() => { fetchHistory(); }, 500); 
    };

    const filteredHistoryTasks = computed(() => { 
        return historyTasks.value; 
    });

    const fetchHistory = async () => {
        try {
            const params = new URLSearchParams();
            if (historyFilter.status) params.append('status', historyFilter.status);
            if (historyFilter.search) params.append('search', historyFilter.search);
            params.append('page', '1'); 
            params.append('page_size', '1000');
            const res = await fetch('/api/tasks?' + params.toString());
            const data = await res.json();
            if (res.ok) { 
                historyTasks.value = (data.tasks || []).map(item => ({ 
                    ...item, 
                    showCrops: false, 
                    cropsLoaded: false, 
                    crops: [] 
                })); 
            }
        } catch (e) { 
            window.ElementPlus.ElMessage.error('获取历史失败: ' + e.message); 
        }
    };

    const deleteTask = async (taskId) => { 
        try { 
            const res = await fetch('/api/tasks/' + taskId, { method: 'DELETE' }); 
            if (res.ok) { 
                fetchHistory(); 
                window.ElementPlus.ElMessage.success('删除成功'); 
            } else throw new Error('删除失败'); 
        } catch (e) { 
            window.ElementPlus.ElMessage.error('删除失败: ' + e.message); 
        } 
    };

    const cleanupCompleted = async () => { 
        try { 
            const res = await fetch('/api/tasks/cleanup', { method: 'POST' }); 
            if (res.ok) { 
                fetchHistory(); 
                window.ElementPlus.ElMessage.success('清理完成'); 
            } else throw new Error('清理失败'); 
        } catch (e) { 
            window.ElementPlus.ElMessage.error('清理失败: ' + e.message); 
        } 
    };

    const downloadHistoryFile = (taskId, type) => { 
        window.open('/api/download/' + taskId + '/' + type, '_blank'); 
    };

    const toggleCrops = async (item) => {
        item.showCrops = !item.showCrops;
        if (item.showCrops && !item.cropsLoaded) {
            try {
                const res = await fetch('/api/tasks/' + item.task_id + '/crops');
                const data = await res.json();
                if (res.ok) { 
                    item.crops = data.crops || []; 
                    item.cropsLoaded = true; 
                }
            } catch (e) { 
                window.ElementPlus.ElMessage.error('获取裁剪记录失败: ' + e.message); 
            }
        }
    };

    const downloadCropFile = (cropId) => { 
        window.open('/api/download/crop/' + cropId, '_blank'); 
    };

    const reprocessTask = async (taskId) => { 
        try { 
            const res = await fetch('/api/tasks/' + taskId + '/reprocess', { method: 'POST' }); 
            const data = await res.json(); 
            if (!res.ok) throw new Error(data.detail || '重新制作失败'); 
            
            steps.forEach(s => { s.active = false; s.completed = false; }); 
            task.value = { task_id: taskId, status: 'processing', step_progress: 0, current_step: '准备开始' }; 
            
            activeTab.value = 'main'; 
            startPolling(taskId); 
            window.ElementPlus.ElMessage.success('已开始重新制作'); 
        } catch (e) { 
            window.ElementPlus.ElMessage.error('重新制作失败: ' + e.message); 
        } 
    };

    return {
        historyTasks,
        historyFilter,
        onSearchInput,
        filteredHistoryTasks,
        fetchHistory,
        deleteTask,
        cleanupCompleted,
        downloadHistoryFile,
        toggleCrops,
        downloadCropFile,
        reprocessTask
    };
}