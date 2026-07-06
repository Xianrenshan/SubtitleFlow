/**
 * useConfig.js - 系统配置模块 Composable
 */
const { ref, reactive } = window.Vue;

export function useConfig() {
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

    const PROVIDER_PRESETS = {
        'openai': { base_url: 'https://api.openai.com', model: 'gpt-3.5-turbo' },
        'deepseek': { base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
        'siliconflow': { base_url: 'https://api.siliconflow.cn', model: 'deepseek-ai/DeepSeek-V3' },
        'zhipu': { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4' },
        'qwen': { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-turbo' },
        'moonshot': { base_url: 'https://api.moonshot.cn', model: 'moonshot-v1-8k' },
    };

    const onProviderChange = (val) => {
        const preset = PROVIDER_PRESETS[val];
        if (preset) {
            configForm.online_api.base_url = preset.base_url;
            configForm.online_api.model = preset.model;
            window.ElementPlus.ElMessage.success(`已切换至 ${val}，URL 与模型已更新`);
        }
    };

    const onFallbackProviderChange = (val, idx) => {
        const preset = PROVIDER_PRESETS[val];
        if (preset) {
            const fb = configForm.online_api.fallbacks[idx];
            if (fb) {
                fb.base_url = preset.base_url;
                fb.model = preset.model;
                window.ElementPlus.ElMessage.success(`备用 ${idx + 1} 已切换至 ${val}，URL 与模型已更新`);
            }
        }
    };

    const onTopicChange = (val) => {
        const topics = { 
            football: 'football match analysis tactics players', 
            f1: 'F1 racing driver circuit lap time strategy', 
            basketball: 'basketball game players tactics score', 
            esports: 'esports game tournament players strategy' 
        };
        if (val && val !== 'custom') configForm.local_translation.topic = topics[val];
        else if (val === 'custom') configForm.local_translation.topic = '';
        else configForm.local_translation.topic = '';
    };

    const addFallback = () => {
        if (!configForm.online_api.fallbacks) configForm.online_api.fallbacks = [];
        configForm.online_api.fallbacks.push({ 
            name: '备用' + (configForm.online_api.fallbacks.length + 1), 
            provider: 'openai', 
            base_url: '', 
            api_key: '', 
            model: '' 
        });
    };

    const removeFallback = (idx) => { 
        configForm.online_api.fallbacks.splice(idx, 1); 
    };

    const saveConfig = async () => {
        try {
            const res = await fetch('/api/config', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json' }, 
                body: JSON.stringify(configForm) 
            });
            if (res.ok) { 
                configSaved.value = true; 
                window.ElementPlus.ElMessage.success('配置保存成功'); 
                setTimeout(() => { configSaved.value = false; }, 2000); 
            } else throw new Error('保存失败');
        } catch (e) { 
            window.ElementPlus.ElMessage.error('保存失败: ' + e.message); 
        }
    };

    const loadConfig = () => {
        fetch('/api/config')
            .then(res => res.json())
            .then(data => { 
                Object.assign(configForm, data); 
                if (!configForm.online_api.fallbacks) configForm.online_api.fallbacks = []; 
            })
            .catch(() => {});
    };

    return {
        configForm,
        topicPreset,
        configSaved,
        onProviderChange,
        onFallbackProviderChange,
        onTopicChange,
        addFallback,
        removeFallback,
        saveConfig,
        loadConfig
    };
}