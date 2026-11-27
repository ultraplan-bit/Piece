(() => {
  const id = __CHART_ID__;
  let attempts = 0;
  const install = () => {
    const component = getElement(id);
    const chart = component?.chart;
    if (!chart) {
      if (++attempts < 80 && component) setTimeout(install, 25);
      return;
    }
    if (chart.isDisposed() || chart.__pieceInteractions) return;
    chart.__pieceInteractions = true;
    const element = chart.getDom();
    let origin = null;
    element.addEventListener('pointerdown', event => { origin = [event.clientX, event.clientY]; }, true);
    element.addEventListener('pointermove', event => {
      if (origin && event.buttons && Math.hypot(event.clientX - origin[0], event.clientY - origin[1]) > 5) {
        window.pieceGraphDraggedUntil = performance.now() + 300;
      }
    }, true);
    element.addEventListener('pointerup', () => { origin = null; }, true);
    element.addEventListener('pointercancel', () => { origin = null; }, true);
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) chart.setOption({animation: false});
    let pending = 0;
    let roamTimer = 0;
    let applyingLabels = false;
    let hierarchySize = '';
    const updateLabels = () => {
      pending = 0;
      if (chart.isDisposed()) return;
      const series = chart.getOption().series?.[0];
      if (series?.type !== 'graph') return;
      if (series.pieceHierarchy) {
        const size = `${chart.getWidth()}:${chart.getHeight()}:${window.innerWidth}:${window.innerHeight}`;
        if (hierarchySize !== size) {
          hierarchySize = size;
          element.dispatchEvent(new CustomEvent('hierarchy-size', {detail: {width: chart.getWidth(), height: chart.getHeight(),
            windowWidth: window.innerWidth, windowHeight: window.innerHeight}}));
        }
        // 卡片中的两行标题始终可读；不能用散点图的标签容量规则把它隐藏。
        return;
      }
      hierarchySize = '';
      const zoom = series.zoom || 1;
      // 标签由实际画布的可用面积与缩放共同决定；hideOverlap 做最终碰撞避让。
      const capacity = Math.max(8, chart.getWidth() * chart.getHeight() / 6500 * zoom * zoom);
      const modelData = chart.getModel().getSeriesByIndex(0).getData();
      const bounds = graphic => {
        if (!graphic) return null;
        const transform = graphic.getComputedTransform();
        const box = graphic.getBoundingRect().clone();
        box.applyTransform(transform);
        return box;
      };
      const symbols = series.pieceTopology ? series.data.map((_, index) =>
        bounds(modelData.getItemGraphicEl(index)?.getSymbolPath())) : [];
      const overlaps = (a, b) => a.x < b.x + b.width + 2 && a.x + a.width > b.x - 2 &&
        a.y < b.y + b.height + 2 && a.y + a.height > b.y - 2;
      let changed = false;
      const nodes = series.data.map((node, index) => {
        const base = node.pieceLabelBase ?? !!node.label?.show;
        let show = node.kind === 'group' || series.data.length <= capacity || (zoom >= 0.65 && base);
        const basePosition = node.pieceLabelPosition ?? node.label?.position;
        let labelPosition = basePosition;
        if (show && series.pieceTopology) {
          const symbol = symbols[index];
          const text = bounds(modelData.getItemGraphicEl(index)?.getSymbolPath()?.getTextContent());
          show = false;
          if (symbol && text) {
            // 在实际像素坐标中找可读的一侧；长标题不能盖住其他节点或被画布裁切。
            const gap = 7;
            const cx = symbol.x + symbol.width / 2, cy = symbol.y + symbol.height / 2;
            const candidates = {
              right: {x: symbol.x + symbol.width + gap, y: cy - text.height / 2},
              left: {x: symbol.x - gap - text.width, y: cy - text.height / 2},
              bottom: {x: cx - text.width / 2, y: symbol.y + symbol.height + gap},
              top: {x: cx - text.width / 2, y: symbol.y - gap - text.height},
            };
            for (const side of new Set([basePosition, 'right', 'left', 'bottom', 'top'])) {
              if (!candidates[side]) continue;
              const box = {...candidates[side], width: text.width, height: text.height};
              if (box.x < 3 || box.y < 3 || box.x + box.width > chart.getWidth() - 3 ||
                  box.y + box.height > chart.getHeight() - 3) continue;
              if (symbols.some((other, i) => i !== index && other && overlaps(box, other))) continue;
              show = true;
              labelPosition = side;
              break;
            }
          }
        }
        if (show !== !!node.label?.show || labelPosition !== node.label?.position) changed = true;
        const point = modelData.getItemLayout(index);
        return {...node, pieceLabelBase: base, pieceLabelPosition: basePosition,
          x: point?.[0] ?? node.x, y: point?.[1] ?? node.y,
          label: {...node.label, show, position: labelPosition}};
      });
      if (changed) {
        applyingLabels = true;
        try {
          // 标签收敛不启动节点/连线动画，也不覆盖刚刚完成的缩放和平移。
          chart.setOption({series: [{id: series.id, animationDurationUpdate: 0, data: nodes}]}, {silent: true});
        } finally {
          applyingLabels = false;
        }
      }
    };
    const schedule = () => {
      if (!pending && !roamTimer && !applyingLabels) pending = requestAnimationFrame(updateLabels);
    };
    const deferLabels = () => {
      // 原生 roam 只变换视口；滚动过程中 setOption 会打断它并重跑整图渲染。
      cancelAnimationFrame(pending);
      pending = 0;
      clearTimeout(roamTimer);
      roamTimer = setTimeout(() => { roamTimer = 0; schedule(); }, 160);
    };
    chart.on('graphroam', deferLabels);
    let wheelFrame = 0;
    let wheelDelta = 0;
    let wheelOrigin = [0, 0];
    const wheel = event => {
      const series = chart.getModel()?.getSeriesByIndex(0);
      if (!series?.get('pieceTopology') || !Number.isFinite(event.deltaY) || !event.deltaY) return;
      // 原生滚轮最小跳变 10%；按像素连续缩放，每帧合并触控板的细小输入。
      event.preventDefault();
      event.stopImmediatePropagation();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? chart.getHeight() : 1;
      wheelDelta += event.deltaY * unit;
      const rect = element.getBoundingClientRect();
      wheelOrigin = [(event.clientX - rect.left) * chart.getWidth() / rect.width,
                     (event.clientY - rect.top) * chart.getHeight() / rect.height];
      deferLabels();
      if (!wheelFrame) wheelFrame = requestAnimationFrame(() => {
        wheelFrame = 0;
        const zoom = Math.exp(Math.max(-1, Math.min(1, -wheelDelta * 0.001)));
        wheelDelta = 0;
        const current = !chart.isDisposed() && chart.getModel()?.getSeriesByIndex(0);
        if (current?.id === series.id && current.get('pieceTopology')) {
          chart.dispatchAction({type: 'graphRoam', seriesId: series.id,
            zoom, originX: wheelOrigin[0], originY: wheelOrigin[1]});
        }
      });
    };
    element.addEventListener('wheel', wheel, {capture: true, passive: false});
    chart.on('finished', schedule);
    const resizeObserver = new ResizeObserver(schedule);
    resizeObserver.observe(element);
    const dispose = chart.dispose.bind(chart);
    chart.dispose = () => {
      clearTimeout(roamTimer);
      cancelAnimationFrame(pending);
      cancelAnimationFrame(wheelFrame);
      element.removeEventListener('wheel', wheel, true);
      resizeObserver.disconnect();
      dispose();
    };
    schedule();
  };
  install();
})();
