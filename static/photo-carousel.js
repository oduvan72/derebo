(() => {
    // Защита от двойной привязки слушателей, если скрипт подключён дважды.
    if (window.__photoCarouselBooted) return;
    window.__photoCarouselBooted = true;

    const pointerStarts = new Map();

    const moveSlide = (carousel, direction) => {
        const track = carousel.querySelector('.photo-carousel__track');
        const slides = track?.querySelectorAll('.photo-carousel__slide');
        const count = slides?.length ?? 0;
        if (!track || count < 2) return;

        const activeIndex = Number(track.dataset.activeIndex || 0);
        const nextIndex = (activeIndex + direction + count) % count;
        track.dataset.activeIndex = nextIndex;
        track.style.transform = `translateX(-${nextIndex * 100}%)`;

        const counter = carousel.querySelector('[data-carousel-count]');
        if (counter) counter.textContent = `${nextIndex + 1} / ${count}`;
    };

    document.addEventListener('click', (event) => {
        const button = event.target.closest(
            '[data-carousel-prev], [data-carousel-next]',
        );
        if (!button) return;

        const carousel = button.closest('[data-photo-carousel]');
        if (!carousel) return;
        moveSlide(carousel, button.hasAttribute('data-carousel-next') ? 1 : -1);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        const target = event.target;
        if (!(target instanceof Element)) return;
        const carousel = target.closest('[data-photo-carousel]');
        if (!carousel || target.closest('button')) return;

        event.preventDefault();
        moveSlide(carousel, event.key === 'ArrowRight' ? 1 : -1);
    });

    document.addEventListener('pointerdown', (event) => {
        const carousel = event.target.closest('[data-photo-carousel]');
        if (
            !carousel ||
            !event.target.closest('.photo-carousel__slides') ||
            event.target.closest('button') ||
            (event.pointerType === 'mouse' && event.button !== 0)
        ) {
            return;
        }

        carousel.setPointerCapture(event.pointerId);
        pointerStarts.set(event.pointerId, {
            carousel,
            x: event.clientX,
            y: event.clientY,
        });
    });

    document.addEventListener('pointerup', (event) => {
        const start = pointerStarts.get(event.pointerId);
        if (!start) return;
        pointerStarts.delete(event.pointerId);

        const distanceX = event.clientX - start.x;
        const distanceY = event.clientY - start.y;
        if (Math.abs(distanceX) < 40 || Math.abs(distanceX) <= Math.abs(distanceY)) return;
        moveSlide(start.carousel, distanceX < 0 ? 1 : -1);
    });

    const clearPointerStart = (event) => pointerStarts.delete(event.pointerId);
    document.addEventListener('pointercancel', clearPointerStart);
    document.addEventListener('lostpointercapture', clearPointerStart);
})();
