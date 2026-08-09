/**
 * useHistory.js - 历史记录查询与重试管理 Composable
 *
 * 变更：reprocessTask 改为调用入队 API（POST /tasks/{id}/reprocess）
 * 而非直接启动流水线。入队后刷新任务队列。
 */
const { ref, reactive, computed } = window.Vue;

export function useHistory(task, steps, startPolling, resetMain, activeTab, fetchTaskQueue) {
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
            params.append('page_size', '100');

            const res = await fetch('/api/tasks?' + params.toString());
            const data = await res.json();
            if (res.ok) {
                historyTasks.value = data.tasks || [];
            }
        } catch (e) {
            console.error('获取历史记录失败:', e);
        }
    };

    const deleteTask = async (taskId) => {
        try {
            const res = await fetch('/api/tasks/' + taskId, { method: 'DELETE' });
            if (res.ok) {
                window.ElementPlus.ElMessage.success('已删除');
                await fetchHistory();
            }
        } catch (e) {
            window.ElementPlus.ElMessage.error('删除失败: ' + e.message);
        }
    };

    const cleanupCompleted = async () => {
        try {
            const res = await fetch('/api/tasks/cleanup-completed', { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                window.ElementPlus.ElMessage.success('已清理 ' + data.deleted + ' 条记录');
                await fetchHistory();
            }
        } catch (e) {
            window.ElementPlus.ElMessage.error('清理失败: ' + e.message);
        }
    };

    const downloadHistoryFile = (task, fileType) => {
        const taskId = task.task_id;
        const url = '/api/download/' + taskId + '/' + fileType;
        window.open(url, '_blank');
    };

    const toggleCrops = (taskId) => {
        const task = historyTasks.value.find(t => t.task_id === taskId);
        if (task) {
            task.showCrops = !task.showCrops;
            if (task.showCrops && !task.crops) {
                fetchCrops(taskId);
            }
        }
    };

    const fetchCrops = async (taskId) => {
        try {
            const res = await fetch('/api/crop/' + taskId);
            const data = await res.json();
            if (res.ok) {
                const task = historyTasks.value.find(t => t.task_id === taskId);
                if (task) task.crops = data.crops || [];
            }
        } catch (e) {
            console.error('获取裁剪记录失败:', e);
        }
    };

    const downloadCropFile = (cropId) => {
        window.open('/api/crop/' + cropId + '/download', '_blank');
    };

    const reprocessTask = async (taskId) => {
        try {
            const res = await fetch('/api/tasks/' + taskId + '/reprocess', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '重新制作失败');

            // 入队成功，刷新队列
            window.ElementPlus.ElMessage.success('已加入队列，等待处理');

            // 切换到处理中心
            activeTab.value = 'main';

            // 刷新任务队列
            if (fetchTaskQueue) {
                await fetchTaskQueue();
            }
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
        reprocessTask,
    };
}
