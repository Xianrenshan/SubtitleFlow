/**
 * db.js - IndexedDB 本地断点续传状态存储适配层
 */
export const uploadDB = {
    db: null,
    
    async init() {
        return new Promise((resolve) => {
            const request = indexedDB.open('SubtitleFlowUploads', 1);
            
            request.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains('pending_uploads')) {
                    db.createObjectStore('pending_uploads', { keyPath: 'uploadId' });
                }
            };
            
            request.onsuccess = (e) => {
                this.db = e.target.result;
                resolve();
            };
            
            request.onerror = () => {
                console.warn('IndexedDB 不可用，断点续传功能关闭');
                resolve();
            };
        });
    },
    
    async save(state) {
        if (!this.db) return;
        try {
            const tx = this.db.transaction('pending_uploads', 'readwrite');
            tx.objectStore('pending_uploads').put(state);
        } catch (e) {
            console.warn('IDB save fail', e);
        }
    },
    
    async get(uploadId) {
        if (!this.db) return null;
        try {
            const tx = this.db.transaction('pending_uploads', 'readonly');
            const req = tx.objectStore('pending_uploads').get(uploadId);
            return new Promise((r) => {
                req.onsuccess = () => r(req.result || null);
                req.onerror = () => r(null);
            });
        } catch (e) {
            return null;
        }
    },
    
    async getAll() {
        if (!this.db) return [];
        try {
            const tx = this.db.transaction('pending_uploads', 'readonly');
            const req = tx.objectStore('pending_uploads').getAll();
            return new Promise((r) => {
                req.onsuccess = () => r(req.result || []);
                req.onerror = () => r([]);
            });
        } catch (e) {
            return [];
        }
    },
    
    async remove(uploadId) {
        if (!this.db) return;
        try {
            const tx = this.db.transaction('pending_uploads', 'readwrite');
            tx.objectStore('pending_uploads').delete(uploadId);
        } catch (e) {
            console.warn('IDB remove fail', e);
        }
    }
};