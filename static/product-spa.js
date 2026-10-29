(() => {
    const main = document.querySelector('#main');
    if (!main || !window.history.pushState) return;

    let activeNavigation;

    const saveCurrentPage = () => {
        history.replaceState({
            main: main.innerHTML,
            title: document.title,
        }, '', window.location.href);
    };

    const focusMainHeading = () => {
        const heading = main.querySelector('h1');
        if (!heading) return;
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
    };

    const mountPage = (markup, title) => {
        main.innerHTML = markup;
        document.title = title;
        window.scrollTo(0, 0);
        focusMainHeading();
    };

    const restorePage = (page) => mountPage(page.main, page.title);

    if (!history.state || typeof history.state.main !== 'string') {
        saveCurrentPage();
    }

    document.addEventListener('click', async (event) => {
        const link = event.target.closest('a[data-spa-link]');
        if (
            !link ||
            event.defaultPrevented ||
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey ||
            link.target ||
            new URL(link.href, window.location.href).origin !== window.location.origin
        ) {
            return;
        }

        event.preventDefault();
        activeNavigation?.abort();
        const controller = new AbortController();
        activeNavigation = controller;

        let response;
        let markup;
        try {
            response = await fetch(link.href, {
                headers: { 'X-SPA-Request': 'true' },
                signal: controller.signal,
            });
            if (response.ok) markup = await response.text();
        } catch (error) {
            if (controller.signal.aborted) return;
            console.error('Не удалось выполнить переход без перезагрузки.', error);
            window.location.assign(link.href);
            return;
        }

        if (controller.signal.aborted) return;
        if (activeNavigation === controller) activeNavigation = undefined;
        if (!response.ok) {
            window.location.assign(link.href);
            return;
        }

        saveCurrentPage();
        history.pushState(null, '', link.href);
        mountPage(
            markup,
            `${main.querySelector('h1')?.textContent || 'Страница'} — Древо Эльтои`,
        );
        saveCurrentPage();
    });

    window.addEventListener('popstate', (event) => {
        activeNavigation?.abort();
        activeNavigation = undefined;

        const page = event.state;
        if (!page || typeof page.main !== 'string') {
            window.location.reload();
            return;
        }

        restorePage(page);
    });
})();
