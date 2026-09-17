({ dateText, slotTexts, action }) => {
  const text = element => (element?.innerText || '').replace(/\s+/g, ' ').trim();
  const visible = element => !!element?.getClientRects().length
    && getComputedStyle(element).visibility !== 'hidden';
  const cells = new Map();

  // The site's calendar is a set of columns, not a table. Match the date
  // column and time-label index directly; never guess a cell by coordinates.
  for (const calendar of document.querySelectorAll('.week_calendar')) {
    if (!visible(calendar)) continue;
    const column = Array.from(calendar.querySelectorAll('.reservation_data')).find(
      item => visible(item) && text(item.querySelector('.week_header')).includes(dateText)
    );
    if (!column) continue;
    const labels = Array.from(calendar.querySelectorAll('.left_time > dd'));
    const entries = Array.from(column.querySelectorAll(':scope > dd'));
    if (labels.length !== entries.length) continue; // Still rendering.
    labels.forEach((label, index) => cells.set(text(label), entries[index]));
    break;
  }

  // Keep a table adapter for alternate page layouts and local fixtures.
  if (!cells.size) {
    for (const table of document.querySelectorAll('table')) {
      if (!visible(table)) continue;
      const rows = Array.from(table.rows);
      const headerIndex = rows.findIndex(row => Array.from(row.cells).some(
        cell => text(cell).includes(dateText)
      ));
      if (headerIndex < 0) continue;
      const columnIndex = Array.from(rows[headerIndex].cells).findIndex(
        cell => text(cell).includes(dateText)
      );
      for (const row of rows.slice(headerIndex + 1)) {
        cells.set(text(row.cells[0]), row.cells[columnIndex]);
      }
      break;
    }
  }

  const blocking = Array.from(document.querySelectorAll(
    '.el-loading-mask, .el-dialog__wrapper, .el-message-box__wrapper, [role="dialog"]'
  )).some(visible);
  const inspect = slot => {
    const cell = cells.get(slot);
    if (!cell || !visible(cell)) return { status: 'not-found', text: '' };
    const label = text(cell);
    if (blocking) return { status: 'not-ready', text: '页面仍在加载或有弹窗遮挡' };
    if (/(未开放|已过期|已满|约满|已预约|不可预约|停用|关闭|无余量|暂无|冲突)/.test(label)
      || cell.matches('[disabled], .disabled, .is-disabled, .no_active, [aria-disabled="true"]')
      || cell.querySelector('[disabled], .disabled, .is-disabled, [aria-disabled="true"]')) {
      return { status: 'unavailable', text: label };
    }
    // Blank/loading/unknown cells must never be submitted.
    if (!label.includes('可预约')) return { status: 'not-ready', text: label };
    return { status: 'available', text: label };
  };
  if (action === 'locate') {
    const slot = slotTexts[0];
    if (inspect(slot).status !== 'available') return null;
    const cell = cells.get(slot);
    const target = Array.from(cell.querySelectorAll('button, a, [role="button"]'))
      .find(element => visible(element) && !element.disabled) || cell;
    return target;
  }
  return Object.fromEntries(slotTexts.map(slot => [slot, inspect(slot)]));
}
