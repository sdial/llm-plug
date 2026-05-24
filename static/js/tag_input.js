class TagInput {
    constructor(containerId, hiddenInputId, placeholder = '输入模型名称') {
this.container = document.getElementById(containerId);
this.hiddenInput = document.getElementById(hiddenInputId);
this.tags = [];
this.placeholder = placeholder;
this._input = null;
// Click-to-focus listener added once
if (this.container) {
    this.container.addEventListener('click', () => this._input?.focus());
}
this.render();
    }

    setTags(tags) {
this.tags = Array.isArray(tags) ? [...tags] : [];
this.syncHidden();
this.render();
    }

    getTags() {
return [...this.tags];
    }

    addTag(tag) {
const t = tag?.trim();
if (t && !this.tags.includes(t)) {
    this.tags.push(t);
    this.syncHidden();
    this.render();
}
    }

    removeTag(tag) {
this.tags = this.tags.filter(t => t !== tag);
this.syncHidden();
this.render();
    }

    syncHidden() {
if (this.hiddenInput) {
    this.hiddenInput.value = this.tags.join(', ');
}
    }

    render() {
if (!this.container) return;
this.container.innerHTML = '';
this.container.className = 'tag-input-container';

// 渲染现有 tags
this.tags.forEach(tag => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.innerHTML = `${this._esc(tag)}<span class="tag-chip-remove" data-tag="${this._esc(tag)}">&times;</span>`;
    chip.querySelector('.tag-chip-remove').addEventListener('click', (e) => {
        e.stopPropagation();
        this.removeTag(tag);
    });
    this.container.appendChild(chip);
});

// 输入框
const input = document.createElement('input');
input.type = 'text';
input.className = 'tag-input-field';
input.placeholder = this.tags.length ? '' : this.placeholder;
input.addEventListener('keydown', (e) => this._onKeydown(e, input));
this._input = input;
this.container.appendChild(input);
    }

    _onKeydown(e, input) {
const value = input.value.trim();
if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    if (value) {
        this.addTag(value);
        input.value = '';
    }
} else if (e.key === 'Backspace' && !value && this.tags.length) {
    e.preventDefault();
    this.tags.pop();
    this.syncHidden();
    this.render();
}
    }

    _esc(s) {
return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

window.TagInput = TagInput;
