    let activeTab = 'search';
    let currentCategory = 'All';
    let isCloudMode = false;
    let isResolvingNewDownload = false;
    let activeSearchController = null;
    let activeSearchEventSource = null;
    let downloadsInterval = null;
    let allFetchedResults = [];
    let lastSearchQuery = '';
    let trendingMoviesList = [];

    // 1-week TTL search result cache using localStorage
    const SEARCH_CACHE_PREFIX = 'mcrackd_search_';
    const SEARCH_CACHE_TTL = 7 * 24 * 60 * 60 * 1000; // 1 week in ms

    function getSearchCache(query) {
      try {
        const raw = localStorage.getItem(SEARCH_CACHE_PREFIX + query.toLowerCase());
        if (!raw) return null;
        const cached = JSON.parse(raw);
        if (Date.now() - cached.ts > SEARCH_CACHE_TTL) {
          localStorage.removeItem(SEARCH_CACHE_PREFIX + query.toLowerCase());
          return null;
        }
        return cached.results;
      } catch (e) { return null; }
    }

    function setSearchCache(query, results) {
      try {
        localStorage.setItem(SEARCH_CACHE_PREFIX + query.toLowerCase(), JSON.stringify({
          ts: Date.now(),
          results: results
        }));
      } catch (e) { /* localStorage full — silently fail */ }
    }

    // Startup Init
    window.addEventListener('DOMContentLoaded', () => {

      // Close IMDb autocomplete dropdown when clicking outside
      document.addEventListener('click', (e) => {
        const wrap = document.querySelector('.search-input-wrap');
        if (wrap && !wrap.contains(e.target)) {
          hideSuggestions();
        }
      });

      // Start polling status & downloads
      pollStatus();
      pollDownloads();
      downloadsInterval = setInterval(() => {
        pollDownloads();
        pollStatus();
      }, 1000);

      // Hide status footer in cloud mode after first poll
      setTimeout(() => {
        if (isCloudMode) {
          const footer = document.getElementById('status-bar-footer');
          if (footer) footer.style.display = 'none';
        }
      }, 1500);

      // Forcefully prevent pinch-to-zoom gestures on mobile devices
      document.addEventListener('touchstart', (event) => {
        if (event.touches.length > 1) {
          event.preventDefault();
        }
      }, { passive: false });

      // Forcefully prevent double-tap-to-zoom gestures on mobile devices
      let lastTouchEnd = 0;
      document.addEventListener('touchend', (event) => {
        const now = (new Date()).getTime();
        if (now - lastTouchEnd <= 300) {
          event.preventDefault();
        }
        lastTouchEnd = now;
      }, { passive: false });

      // High-performance Instant Search Box event listeners
      const searchBox = document.getElementById('search-box');
      if (searchBox) {
        searchBox.addEventListener('keydown', onSearchKeydown);
        searchBox.addEventListener('input', onSearchInput);
        searchBox.addEventListener('focus', onSearchFocus);
      }

      // Fetch and render trending showcase marquee on home page
      fetchAndRenderTrendingShowcase();
    });

    function goHome() {
      // 1. Reset direct view navigation if nested
      if (typeof goBackToMovie === 'function') {
        goBackToMovie();
      }
      // 2. Close details pane if active
      if (typeof closeDetails === 'function') {
        closeDetails();
      }
      // 3. Switch back to search engine home tab
      switchTab('search');
    }

    function switchTab(tab) {
      activeTab = tab;
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-panel').forEach(panel => panel.classList.remove('active'));
      
      if (tab === 'search') {
        document.querySelector('.tab-btn:nth-child(1)').classList.add('active');
        document.getElementById('search-view').classList.add('active');
      } else {
        document.querySelector('.tab-btn:nth-child(2)').classList.add('active');
        document.getElementById('direct-view').classList.add('active');
        
        // Hide details and cleanly restore search view elements under-the-hood
        closeDetails();
        
        // Reset Direct tab back button & show the direct link input row
        document.getElementById('direct-back-row').style.display = 'none';
        document.querySelector('.direct-input-row').style.display = 'flex';
      }
    }

    // ── Showcase Marquee API & Rendering ──
    function fetchAndRenderTrendingShowcase() {
      fetch('/api/trending')
        .then(r => r.json())
        .then(data => {
          if (data.movies && data.movies.length > 0) {
            trendingMoviesList = data.movies;
            renderTrendingShowcase();
          }
        })
        .catch(err => console.error("Failed to fetch trending movies:", err));
    }

    function renderTrendingShowcase() {
      const q = document.getElementById('search-box').value.trim();
      if (q.length >= 2) return; // Ignore if user is actively searching

      const resultsDiv = document.getElementById('search-results');
      if (!resultsDiv) return;

      resultsDiv.style.display = 'block';

      if (!trendingMoviesList || trendingMoviesList.length === 0) {
        // Render 3 skeleton marquee rows while fetching
        let rowsHtml = '';
        for (let r = 0; r < 3; r++) {
          const direction = (r % 2 === 0) ? 'left' : 'right';
          const skeletonsHtml = Array.from({length: 8}).map(() => `
            <div class="movie-card static-overlay skeleton-card" style="border: none;">
              <div class="poster-wrap">
                <div class="skeleton-img"></div>
              </div>
            </div>
          `).join('');
          
          rowsHtml += `
            <div class="marquee-row-wrapper">
              <div class="marquee-track ${direction}">
                <div class="marquee-group">${skeletonsHtml}</div>
                <div class="marquee-group">${skeletonsHtml}</div>
              </div>
            </div>
          `;
        }
        resultsDiv.innerHTML = `
          <div class="trending-showcase-container">
            ${rowsHtml}
          </div>
        `;
        return;
      }

      // Split into 3 rows
      const rowCount = 3;
      const moviesPerRow = Math.ceil(trendingMoviesList.length / rowCount);
      let rowsHtml = '';

      for (let r = 0; r < rowCount; r++) {
        const start = r * moviesPerRow;
        const rowMovies = trendingMoviesList.slice(start, start + moviesPerRow);
        if (rowMovies.length === 0) continue;

        const direction = (r % 2 === 0) ? 'left' : 'right';
        
        const cardsHtml = rowMovies.map(movie => {
          const catClass = movie.category.toLowerCase() === 'animeflix' ? 'anime' : movie.category.toLowerCase();
          const categoryBadge = `<span class="cat-badge ${catClass}">${movie.category === 'ANIMEFLIX' ? 'ANIME' : movie.category}</span>`;
          
          const titleRaw = movie.title;
          let mainTitle = titleRaw.split(/[({\[]/)[0].trim();
          if (!mainTitle) mainTitle = titleRaw;
          
          let extraDetails = titleRaw.substring(mainTitle.length).trim();
          
          return `
            <div class="movie-card static-overlay" data-category="${movie.category}" onclick="event.stopPropagation(); handleShowcaseCardClick(this, '${encodeURIComponent(JSON.stringify(movie))}')">
              <div class="poster-wrap">
                ${movie.thumbnail ? `<img src="/api/thumbnail?url=${encodeURIComponent(movie.thumbnail)}" class="poster-img" loading="eager">` : `<div class="poster-placeholder"><i class="fa fa-film"></i></div>`}
                <div class="poster-hover-overlay">
                  <div class="hover-overlay-content">
                    <span class="hover-overlay-main-title">${mainTitle}</span>
                    ${extraDetails ? `<span class="hover-overlay-extra">${extraDetails}</span>` : ''}
                  </div>
                </div>
              </div>
              ${categoryBadge}
            </div>
          `;
        }).join('');

        rowsHtml += `
          <div class="marquee-row-wrapper">
            <div class="marquee-track ${direction}" onclick="toggleMarqueePause(this)">
              <div class="marquee-group">${cardsHtml}</div>
              <div class="marquee-group">${cardsHtml}</div>
            </div>
          </div>
        `;
      }

      resultsDiv.innerHTML = `
        <div class="trending-showcase-container">
          ${rowsHtml}
        </div>
      `;

      // Apply current category filter if one is active
      filterShowcaseByCategory(currentCategory);
      
      // Initialize dynamic high-performance interactive marquees!
      initInteractiveMarquees();
    }

    function filterShowcaseByCategory(category) {
      document.querySelectorAll('.movie-card.static-overlay').forEach(card => {
        const cat = card.getAttribute('data-category');
        if (category === 'All' || cat === category.toUpperCase() || (category === 'Anime' && cat === 'ANIMEFLIX')) {
          card.classList.remove('hidden-card');
        } else {
          card.classList.add('hidden-card');
        }
      });
    }

    function toggleMarqueePause(track) {
      track.classList.toggle('paused');
    }

    // Registry of active marquee cleanup functions — cancelled before re-init
    let _marqueeCleanups = [];

    function initInteractiveMarquees() {
      // Kill all previous marquee loops + observers before creating new ones
      _marqueeCleanups.forEach(fn => fn());
      _marqueeCleanups = [];

      // Cache hover-device detection once (doesn't change at runtime)
      const isHoverDevice = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

      const tracks = document.querySelectorAll('.marquee-track');
      tracks.forEach(track => {
        // Prevent duplicate initialization
        if (track.dataset.initialized) return;
        track.dataset.initialized = 'true';

        const isLeft = track.classList.contains('left');
        const baseSpeed = isLeft ? -0.8 : 0.8;
        
        // Cache parent wrapper reference once (avoids DOM traversal every frame)
        const wrapper = track.closest('.marquee-row-wrapper');
        
        let x = 0;
        let velocity = 0;
        let isDragging = false;
        let hasMoved = false;
        let isScrolling = false;
        let trackPaused = false;
        let alive = true; // kill-switch for the tick loop
        
        let startX = 0;
        let startY = 0;
        let startTranslate = 0;
        let lastX = 0;
        let lastTime = 0;
        let cachedWrapDist = 0;

        // Force disable keyframe animations so they don't fight custom translate3d
        track.style.animation = 'none';
        track.style.transition = 'none';

        // Cache wrap distance — recalculate only on resize, not every frame
        // Detects actual CSS gap dynamically instead of hardcoding
        function measureWrapDist() {
          if (!track.isConnected) return;
          const group = track.querySelector('.marquee-group');
          if (!group) { cachedWrapDist = 0; return; }
          const gap = parseFloat(getComputedStyle(track).gap) || 24;
          cachedWrapDist = group.offsetWidth + gap;
        }
        let resizeListener = () => { measureWrapDist(); };
        window.addEventListener('resize', resizeListener);

        function wrapOffset(val) {
          if (cachedWrapDist <= 0) return val;
          val = val % cachedWrapDist;
          if (val > 0) val -= cachedWrapDist;
          return val;
        }

        function onStart(clientX, clientY) {
          isDragging = true;
          hasMoved = false;
          isScrolling = false;
          velocity = 0;
          startX = clientX;
          startY = clientY || 0;
          startTranslate = x;
          lastX = clientX;
          lastTime = performance.now();
          
          window.addEventListener('mousemove', onMouseMoveWindow);
          window.addEventListener('mouseup', onMouseUpWindow);
        }

        function onMove(clientX, clientY, e) {
          if (!isDragging) return;
          
          // Detect horizontal vs vertical scroll swipe intention on touch devices
          if (clientY !== undefined && !isScrolling) {
            const dy = Math.abs(clientY - startY);
            const dx = Math.abs(clientX - startX);
            if (dy > dx && dy > 10) {
              isScrolling = true;
              isDragging = false;
              return;
            }
          }

          if (isScrolling) return;

          // Prevent vertical page scroll jiggle during active horizontal swipe
          if (e && e.cancelable) {
            e.preventDefault();
          }

          const dx = clientX - startX;
          
          if (Math.abs(dx) > 5) {
            hasMoved = true;
            // Clear any active mobile tap highlights only when a real swipe/drag gesture is detected
            if (typeof resetActiveTappedCard === 'function') {
              resetActiveTappedCard();
            }
          }
          
          x = wrapOffset(startTranslate + dx);
          
          const now = performance.now();
          const dt = now - lastTime;
          const dist = clientX - lastX;
          if (dt > 0) {
            const instantVel = (dist / dt) * 16.666;
            velocity = velocity * 0.6 + instantVel * 0.4;
          }
          
          lastX = clientX;
          lastTime = now;
          track.style.transform = 'translate3d(' + x + 'px,0,0)';
        }

        function onEnd() {
          if (!isDragging) return;
          isDragging = false;
          
          // Flag to prevent card click from firing after a real drag
          if (hasMoved) {
            window._marqueeJustDragged = true;
            setTimeout(() => { window._marqueeJustDragged = false; }, 100);
          }
          
          window.removeEventListener('mousemove', onMouseMoveWindow);
          window.removeEventListener('mouseup', onMouseUpWindow);
        }

        function onMouseMoveWindow(e) {
          onMove(e.clientX, e.clientY, e);
        }

        function onMouseUpWindow() {
          onEnd();
        }

        // Mouse Event Listeners
        track.addEventListener('mousedown', (e) => {
          if (e.button !== 0) return;
          onStart(e.clientX, e.clientY);
        });

        // Prevent native browser ghost image dragging behavior
        track.addEventListener('dragstart', (e) => {
          e.preventDefault();
        });

        // Touch Event Listeners
        track.addEventListener('touchstart', (e) => {
          onStart(e.touches[0].clientX, e.touches[0].clientY);
        }, { passive: true });

        track.addEventListener('touchmove', (e) => {
          onMove(e.touches[0].clientX, e.touches[0].clientY, e);
        }, { passive: false });

        track.addEventListener('touchend', () => {
          onEnd();
        });

        // Mirror .paused class changes to a fast boolean (avoids classList.contains per frame)
        const pauseObserver = new MutationObserver(() => {
          trackPaused = track.classList.contains('paused');
        });
        pauseObserver.observe(track, { attributes: true, attributeFilter: ['class'] });

        // Smooth Physics Autoplay Tick Loop
        function tick() {
          // Self-terminate if killed or DOM node was destroyed
          if (!alive || !track.isConnected) return;

          if (!isDragging) {
            // Apply momentum deceleration
            if (Math.abs(velocity) > 0.1) {
              x += velocity;
              velocity *= 0.94;
            } else {
              velocity = 0;
            }

            // Hover-pause only on devices with real mouse hover (cached wrapper ref)
            const isHovered = isHoverDevice && wrapper.matches(':hover');
            
            if (!isHovered && !trackPaused) {
              x += baseSpeed;
            }
            
            x = wrapOffset(x);
            track.style.transform = 'translate3d(' + x + 'px,0,0)';
          }
          
          requestAnimationFrame(tick);
        }

        // Defer initial measurement and loop start to next frame to avoid synchronous layout thrashing
        // style resolutions happen on paint pass, completely avoiding layout thrashing!
        requestAnimationFrame(() => {
          if (!alive || !track.isConnected) return;
          measureWrapDist();
          tick();
          
          // Smooth fade-in once the first frame has successfully calculated layouts & translates.
          // completely hides any layout-settling or frame drops from the user's vision!
          if (wrapper) {
            wrapper.classList.add('visible');
          }
        });

        // Register cleanup for this track
        _marqueeCleanups.push(() => {
          alive = false;
          pauseObserver.disconnect();
          window.removeEventListener('resize', resizeListener);
        });
      });

      // Progressively load all remaining off-screen images after 750ms so visible cards get 100% initial bandwidth
      setTimeout(() => {
        document.querySelectorAll('.lazy-showcase-img').forEach(img => {
          if (img.dataset.src) {
            img.src = img.dataset.src;
            img.removeAttribute('data-src');
            img.classList.remove('lazy-showcase-img');
          }
        });
      }, 750);
    }

    function selectCategory(cat) {
      currentCategory = cat;
      document.querySelectorAll('.cat-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.innerText.includes(cat)) btn.classList.add('active');
      });
      
      const q = document.getElementById('search-box').value.trim();
      
      if (q.length < 2) {
        filterShowcaseByCategory(cat);
        return;
      }
      
      // If we have cached results for the current search query, filter them locally instantly!
      if (allFetchedResults.length > 0 && q.toLowerCase() === lastSearchQuery.toLowerCase()) {
        filterAndRenderResultsLocally(q);
      } else {
        // Otherwise trigger a new search
        triggerSearch();
      }
    }

    function filterAndRenderResultsLocally(query) {
      const resultsDiv = document.getElementById('search-results');
      const detailPanel = document.getElementById('detail-panel');
      
      // Ensure search view is visible only if we aren't currently viewing the details page
      if (detailPanel.style.display !== 'flex') {
        detailPanel.style.display = 'none';
        resultsDiv.style.display = 'grid';
      }

      let filtered = [];
      if (currentCategory === 'All') {
        filtered = [...allFetchedResults];
      } else if (currentCategory === 'Hollywood') {
        filtered = allFetchedResults.filter(item => (item.category || '').toUpperCase() === 'HOLLYWOOD');
      } else if (currentCategory === 'Bollywood') {
        filtered = allFetchedResults.filter(item => (item.category || '').toUpperCase() === 'BOLLYWOOD');
      } else if (currentCategory === 'Anime') {
        filtered = allFetchedResults.filter(item => (item.category || '').toUpperCase() === 'ANIMEFLIX');
      }

      // Sort items based on relevance matching the current query
      filtered.sort((a, b) => {
        const scoreA = getRelevanceScore(a.title, query, a.category);
        const scoreB = getRelevanceScore(b.title, query, b.category);
        return scoreA - scoreB;
      });

      if (filtered.length === 0) {
        resultsDiv.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 40px;">No results found in this category.</div>';
      } else {
        renderGridItems(filtered);
      }
    }

    let activeTappedCard = null;

    function handleShowcaseCardClick(cardElement, movieJsonString) {
      // Ignore clicks that are actually the end of a drag/swipe gesture
      if (window._marqueeJustDragged) return;

      // 1. Desktop view: directly view details
      if (window.innerWidth > 768) {
        viewDetails(movieJsonString);
        return;
      }

      // 2. Mobile view: first tap stops row and reveals details, second tap opens details
      if (activeTappedCard === cardElement) {
        // Second tap on the same card -> Navigate!
        viewDetails(movieJsonString);
        resetActiveTappedCard();
      } else {
        // First tap on a new card or switching cards
        resetActiveTappedCard();

        // Set new active card
        activeTappedCard = cardElement;
        cardElement.classList.add('active-tap');

        // Pause the parent marquee track
        const track = cardElement.closest('.marquee-track');
        if (track) {
          track.classList.add('paused');
        }
      }
    }

    function resetActiveTappedCard() {
      if (activeTappedCard) {
        activeTappedCard.classList.remove('active-tap');
        
        // Resume any paused marquee tracks
        const track = activeTappedCard.closest('.marquee-track');
        if (track) {
          track.classList.remove('paused');
        }
        
        activeTappedCard = null;
      }
    }

    // Global click listener to reset tapped cards when clicking elsewhere
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.movie-card.static-overlay')) {
        resetActiveTappedCard();
      }
    }, true);

    let currentSuggestions = [];
    let activeSuggestionIndex = -1;
    
    // Persistent localStorage client cache for IMDb suggestions
    const IMDB_SUGGESTIONS_CACHE_KEY = 'mcrackd_imdb_suggestions';
    let suggestionsClientCache = {};
    try {
      const saved = localStorage.getItem(IMDB_SUGGESTIONS_CACHE_KEY);
      if (saved) {
        suggestionsClientCache = JSON.parse(saved);
      }
    } catch (e) {
      suggestionsClientCache = {};
    }

    function saveSuggestionsToLocalStorage() {
      try {
        const keys = Object.keys(suggestionsClientCache);
        if (keys.length > 2000) {
          // Keep cache size bounded: evict oldest 200 items
          for (let i = 0; i < 200; i++) {
            delete suggestionsClientCache[keys[i]];
          }
        }
        localStorage.setItem(IMDB_SUGGESTIONS_CACHE_KEY, JSON.stringify(suggestionsClientCache));
      } catch (e) {}
    }

    let activeSuggestController = null;

    function clearSearch() {
      const searchBox = document.getElementById('search-box');
      if (searchBox) {
        searchBox.value = '';
        toggleClearButton();
        triggerSearch();
        hideSuggestions();
      }
    }

    function toggleClearButton() {
      const searchBox = document.getElementById('search-box');
      const clearBtn = document.getElementById('clear-search-btn');
      if (searchBox && clearBtn) {
        if (searchBox.value.length > 0) {
          clearBtn.style.display = 'flex';
        } else {
          clearBtn.style.display = 'none';
        }
      }
    }

    // Debounced Search triggers
    let searchDebounceTimer = null;
    function onSearchKeydown(e) {
      // Keyboard Navigation for Suggestions Dropdown (handled instantly on keydown for 60fps responsiveness)
      if (e.key === 'ArrowDown') {
        if (currentSuggestions.length > 0) {
          e.preventDefault(); // Lock cursor inside input
          activeSuggestionIndex = (activeSuggestionIndex + 1) % currentSuggestions.length;
          highlightSuggestion();
        }
        return;
      }
      
      if (e.key === 'ArrowUp') {
        if (currentSuggestions.length > 0) {
          e.preventDefault(); // Lock cursor inside input
          activeSuggestionIndex = (activeSuggestionIndex - 1 + currentSuggestions.length) % currentSuggestions.length;
          highlightSuggestion();
        }
        return;
      }

      if (e.key === 'Enter') {
        if (activeSuggestionIndex >= 0 && activeSuggestionIndex < currentSuggestions.length) {
          e.preventDefault();
          selectSuggestion(activeSuggestionIndex);
        } else {
          clearTimeout(searchDebounceTimer);
          triggerSearch();
          hideSuggestions();
        }
        return;
      }

      if (e.key === 'Escape') {
        hideSuggestions();
        return;
      }
    }

    function onSearchInput(e) {
      const q = e.target.value;
      
      // Auto-trigger suggestions on change instantly (input event handles delete, paste, backspace, etc.)
      handleSuggestions(q);

      toggleClearButton();
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(triggerSearch, 500);
    }

    function onSearchFocus(e) {
      const q = e.target.value;
      if (q.trim().length >= 2) {
        const cacheKey = q.trim().toLowerCase();
        // If we have cached results from before, render them instantly without a network call
        if (suggestionsClientCache[cacheKey] && suggestionsClientCache[cacheKey].length > 0) {
          currentSuggestions = suggestionsClientCache[cacheKey];
          activeSuggestionIndex = -1;
          renderSuggestionsDropdown();
        } else {
          handleSuggestions(q);
        }
      }
    }

    function handleSuggestions(query) {
      const dropdownEl = document.getElementById('imdb-suggestions');
      if (!dropdownEl) return;

      if (query.length < 2) {
        if (activeSuggestController) {
          activeSuggestController.abort();
          activeSuggestController = null;
        }
        currentSuggestions = [];
        activeSuggestionIndex = -1;
        dropdownEl.style.display = 'none';
        dropdownEl.innerHTML = '';
        return;
      }

      const cacheKey = query.trim().toLowerCase();

      // Zero-latency instant rendering if exact match cached client-side!
      if (suggestionsClientCache[cacheKey]) {
        if (activeSuggestController) {
          activeSuggestController.abort();
          activeSuggestController = null;
        }
        currentSuggestions = suggestionsClientCache[cacheKey];
        activeSuggestionIndex = -1;
        renderSuggestionsDropdown();
        return;
      }

      // Local prefix subset filtering: if 'inc' is cached, filter it for 'ince' instantly
      // without making a network call — only fetch from server if the filtered set is empty
      const prefixKeys = Object.keys(suggestionsClientCache)
        .filter(k => cacheKey.startsWith(k))
        .sort((a, b) => b.length - a.length);

      if (prefixKeys.length > 0) {
        const parentResults = suggestionsClientCache[prefixKeys[0]];
        const filtered = parentResults.filter(sug =>
          sug.title.toLowerCase().includes(cacheKey)
        );
        if (filtered.length > 0) {
          // Render the prefix-filtered results instantly
          suggestionsClientCache[cacheKey] = filtered;
          currentSuggestions = filtered;
          activeSuggestionIndex = -1;
          renderSuggestionsDropdown();
          saveSuggestionsToLocalStorage();
          // DON'T return — also fire a background fetch to get exact results for this query
        }
      }

      // Let previous in-flight requests complete in the background to populate the cache!
      // Only show skeleton if no suggestions are currently visible (stale-while-revalidate)
      if (currentSuggestions.length === 0) {
        showSuggestionSkeleton(dropdownEl);
      }

      // Create a NEW AbortController for this fetch (previous ones keep running to cache)
      const controller = new AbortController();
      const signal = controller.signal;
      activeSuggestController = controller;
      
      // Attempt DIRECT IMDb CDN call first (ultra-low latency, typically 10-40ms!)
      const directUrl = `https://v3.sg.media-imdb.com/suggestion/titles/x/${encodeURIComponent(query.toLowerCase())}.json`;
      
      fetch(directUrl, { signal })
        .then(res => {
          if (!res.ok) throw new Error("Direct fetch failed");
          return res.json();
        })
        .then(data => {
          const results = [];
          for (const item of (data.d || [])) {
            if (!item.l) continue;
            results.push({
              id: item.id || '',
              title: item.l || '',
              year: item.y || '',
              stars: item.s || '',
              type: item.q || 'Movie',
              image: item.i?.imageUrl || ''
            });
          }
          const finalResults = results.slice(0, 6);
          suggestionsClientCache[cacheKey] = finalResults;
          saveSuggestionsToLocalStorage();
          
          // Only render if this is still the latest query (prevents older responses overwriting newer ones)
          const currentQuery = document.getElementById('search-box')?.value || '';
          if (currentQuery.trim().toLowerCase() !== cacheKey) return;

          currentSuggestions = finalResults;
          activeSuggestionIndex = -1;
          renderSuggestionsDropdown();
        })
        .catch((err) => {
          if (err.name === 'AbortError') return;
          // Seamless fallback to our Python proxy server if direct fetch is blocked
          fetch(`/api/suggest?q=${encodeURIComponent(query)}`)
            .then(res => res.json())
            .then(data => {
              const results = data || [];
              suggestionsClientCache[cacheKey] = results;
              saveSuggestionsToLocalStorage();
              
              const currentQuery = document.getElementById('search-box')?.value || '';
              if (currentQuery.trim().toLowerCase() !== cacheKey) return;

              currentSuggestions = results;
              activeSuggestionIndex = -1;
              renderSuggestionsDropdown();
            })
            .catch((err2) => {
              if (err2.name === 'AbortError') return;
              // Don't clear visible suggestions on error — keep stale results
            });
        });
    }

    function showSuggestionSkeleton(dropdownEl) {
      const skeletonCount = 4;
      let html = '';
      for (let i = 0; i < skeletonCount; i++) {
        html += `
          <div class="imdb-suggest-item skeleton-item">
            <div class="skeleton-poster skeleton-pulse"></div>
            <div class="imdb-suggest-info">
              <div class="skeleton-title skeleton-pulse"></div>
              <div class="skeleton-meta skeleton-pulse"></div>
            </div>
          </div>
        `;
      }
      dropdownEl.innerHTML = html;
      dropdownEl.style.display = 'flex';
    }

    function renderSuggestionsDropdown() {
      const dropdownEl = document.getElementById('imdb-suggestions');
      if (!dropdownEl) return;

      if (currentSuggestions.length === 0) {
        dropdownEl.style.display = 'none';
        dropdownEl.innerHTML = '';
        return;
      }

      const html = currentSuggestions.map((sug, idx) => {
        // Route poster through server proxy for caching & optimization
        const rawPoster = sug.image || '';
        const posterUrl = rawPoster ? `/api/img-proxy?url=${encodeURIComponent(rawPoster)}` : 'https://images.unsplash.com/photo-1485846234645-a62644f84728?q=80&w=300';
        const typeBadge = sug.type ? `<span class="imdb-suggest-type">${sug.type}</span>` : '';
        const yearInfo = sug.year ? `<span class="imdb-suggest-year">${sug.year}</span>` : '';
        const starsText = sug.stars ? `<span class="imdb-suggest-stars">${escapeHtml(sug.stars)}</span>` : '';

        return `
          <div class="imdb-suggest-item" data-index="${idx}" onclick="selectSuggestion(${idx})">
            <img class="imdb-suggest-poster" src="${posterUrl}" alt="${escapeHtml(sug.title)}" onerror="this.src='https://images.unsplash.com/photo-1485846234645-a62644f84728?q=80&w=300'">
            <div class="imdb-suggest-info">
              <div class="imdb-suggest-title">${escapeHtml(sug.title)}</div>
              <div class="imdb-suggest-meta">
                ${typeBadge}
                ${yearInfo}
                ${starsText}
              </div>
            </div>
          </div>
        `;
      }).join('');

      dropdownEl.innerHTML = html;
      dropdownEl.style.display = 'flex';
    }

    function selectSuggestion(idx) {
      const sug = currentSuggestions[idx];
      if (!sug) return;

      const searchBox = document.getElementById('search-box');
      if (searchBox) {
        searchBox.value = sug.title;
      }
      
      hideSuggestions();
      triggerSearch();
    }

    function hideSuggestions() {
      const dropdownEl = document.getElementById('imdb-suggestions');
      if (dropdownEl) {
        dropdownEl.style.display = 'none';
        dropdownEl.innerHTML = '';
      }
      // Preserve currentSuggestions so focus can re-render them instantly
      activeSuggestionIndex = -1;
    }

    function highlightSuggestion() {
      const dropdownEl = document.getElementById('imdb-suggestions');
      if (!dropdownEl) return;

      const items = dropdownEl.querySelectorAll('.imdb-suggest-item');
      items.forEach((item, idx) => {
        if (idx === activeSuggestionIndex) {
          item.classList.add('keyboard-selected');
          item.scrollIntoView({ block: 'nearest' });
        } else {
          item.classList.remove('keyboard-selected');
        }
      });
    }

    function escapeHtml(str) {
      if (!str) return '';
      return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function triggerSearch() {
      toggleClearButton();
      const q = document.getElementById('search-box').value.trim();
      const resultsDiv = document.getElementById('search-results');
      const detailPanel = document.getElementById('detail-panel');
      
      // Close details and reset background when searching again
      const bgElement = document.getElementById('details-page-bg');
      if (bgElement) bgElement.style.opacity = '0';
      document.querySelector('.search-bar-row').style.display = 'block';
      document.querySelector('.categories-row').style.display = 'flex';
      document.querySelector('.tabs-container').style.display = 'flex';
      
      detailPanel.style.display = 'none';
      resultsDiv.style.display = 'grid';

      // Instantly kill and abort any active search stream and controllers
      if (activeSearchController) {
        activeSearchController.abort();
        activeSearchController = null;
      }
      if (activeSearchEventSource) {
        activeSearchEventSource.close();
        activeSearchEventSource = null;
      }

      if (q.length < 2) {
        allFetchedResults = [];
        lastSearchQuery = '';
        renderTrendingShowcase();
        return;
      }

      // Reset and track new query
      allFetchedResults = [];
      lastSearchQuery = q;

      // Check 1-week TTL localStorage cache — instant results if found!
      const cachedResults = getSearchCache(q);
      if (cachedResults && cachedResults.length > 0) {
        allFetchedResults = cachedResults;
        filterAndRenderResultsLocally(q);
        return;
      }

      // Render search skeletons loading states
      resultsDiv.innerHTML = Array.from({length: 6}).map(() => `
        <div class="movie-card skeleton-card">
          <div class="poster-wrap">
            <div class="skeleton-img"></div>
          </div>
          <div class="movie-details">
            <div class="skeleton-text" style="width: 80%; margin-bottom: 6px;"></div>
            <div class="skeleton-text" style="width: 40%;"></div>
          </div>
        </div>
      `).join('');

      activeSearchController = new AbortController();
      const signal = activeSearchController.signal;
      // We always request 'All' categories from the server to cache them for instantaneous switching!
      const url = `/api/search/stream?q=${encodeURIComponent(q)}&cat=All`;

      // Connect using Server-Sent Events for concurrent realtime streaming!
      activeSearchEventSource = new EventSource(url);
      
      activeSearchEventSource.onmessage = function(event) {
        if (signal.aborted) {
          activeSearchEventSource.close();
          activeSearchEventSource = null;
          return;
        }
        
        try {
          const data = JSON.parse(event.data);
          
          if (data.status === 'done') {
            if (activeSearchEventSource) {
              activeSearchEventSource.close();
              activeSearchEventSource = null;
            }
            if (allFetchedResults.length === 0) {
              resultsDiv.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 40px;">No results found.</div>';
            } else {
              // Save to 1-week TTL localStorage cache for instant repeat searches!
              setSearchCache(q, allFetchedResults);
              // Final filter and redraw altogether
              filterAndRenderResultsLocally(q);
            }
            return;
          }

          if (data.status === 'error') {
            if (activeSearchEventSource) {
              activeSearchEventSource.close();
              activeSearchEventSource = null;
            }
            resultsDiv.innerHTML = `<div style="grid-column: 1/-1; text-align: center; color: var(--crimson); padding: 40px;">Error: ${data.message}</div>`;
            return;
          }

          if (data.item) {
            // Append this item to the cache silently in the background
            const item = data.item;
            allFetchedResults.push(item);
          }
        } catch (e) {
          console.error(e);
        }
      };

      activeSearchEventSource.onerror = function() {
        if (activeSearchEventSource) {
          activeSearchEventSource.close();
          activeSearchEventSource = null;
        }
        if (allFetchedResults.length > 0) {
          filterAndRenderResultsLocally(q);
        } else {
          resultsDiv.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 40px;">Connection closed.</div>';
        }
      };
    }

    function getRelevanceScore(title, query, category) {
      const t = title.toLowerCase();
      const q = query.toLowerCase();
      let score = 100;
      
      if (t === q) score -= 90;
      else if (t.startsWith(q)) score -= 70;
      else if (t.includes(q)) score -= 50;
      
      // Category priorities
      if (category.toLowerCase() === 'hollywood') score -= 5;
      else if (category.toLowerCase() === 'bollywood') score -= 2;
      
      return score;
    }

    function renderGridItems(items) {
      const resultsDiv = document.getElementById('search-results');
      resultsDiv.innerHTML = items.map(item => {
        const titleRaw = item.title;
        // Split on the first bracket, brace, or parentheses to get the crisp, clean movie name
        let mainTitle = titleRaw.split(/[({\[]/)[0].trim();
        if (!mainTitle) mainTitle = titleRaw;
        
        let extraDetails = titleRaw.substring(mainTitle.length).trim();
        
        // Dynamically compute the absolute best main title font size based on its length
        let mainTitleFontSize = 'font-size: 16.5px;';
        if (mainTitle.length > 30) {
          mainTitleFontSize = 'font-size: 13px;';
        } else if (mainTitle.length > 20) {
          mainTitleFontSize = 'font-size: 14.5px;';
        }
        
        // Dynamically compute the absolute best extra details font size based on its length
        let extraFontSize = 'font-size: 11.5px;';
        if (extraDetails.length > 80) {
          extraFontSize = 'font-size: 9px;';
        } else if (extraDetails.length > 50) {
          extraFontSize = 'font-size: 10px;';
        }

        return `
          <div class="movie-card" onclick="viewDetails('${encodeURIComponent(JSON.stringify(item))}')">
            <div class="poster-wrap">
              <span class="cat-badge ${item.category.toLowerCase()}">${item.category}</span>
              ${item.thumbnail ? `<img class="poster-img" src="/api/thumbnail?url=${encodeURIComponent(item.thumbnail)}" loading="lazy" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'">` : ''}
              <div class="poster-placeholder" style="${item.thumbnail ? 'display:none;' : ''}">🎬</div>
            </div>
            <div class="poster-hover-overlay">
              ${item.thumbnail ? `<img class="hover-bg-img" src="/api/thumbnail?url=${encodeURIComponent(item.thumbnail)}" loading="lazy">` : `<div class="hover-bg-img poster-placeholder">🎬</div>`}
              <div class="hover-overlay-gradient"></div>
              <div class="hover-overlay-content">
                <div class="hover-overlay-main-title" style="${mainTitleFontSize}">${mainTitle}</div>
                ${extraDetails ? `<div class="hover-overlay-extra" style="${extraFontSize}">${extraDetails}</div>` : ''}
              </div>
            </div>
            <div class="movie-details">
              <div class="movie-title" title="${item.title}">${item.title}</div>
              <div class="movie-meta">
                <span>ModList</span>
                <span>Online</span>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function parseQualityTitle(title, metadata) {
      let originalTitle = title;
      
      // 1. Size extraction: e.g. [200MB] or (200MB) or [200 MB]
      let size = '';
      const sizeMatch = title.match(/[\[\(](\d+(?:\.\d+)?\s*[kmgt]?i?b)[\]\)]/i);
      if (sizeMatch) {
        size = sizeMatch[1];
        title = title.replace(sizeMatch[0], '').trim();
      }
      
      // 2. Season/Group extraction: e.g. "Season 1", "S01", "Episode 1", "Bonus Episode (Episode 8)", "OVA", "Movie"
      let season = '';
      const seasonMatch = title.match(/(Season\s+\d+|S\d+|\bEpisode\s+\d+|\bEp\s*\d+|\bBonus\s+Episode\s*\(Episode\s*\d+\)|\bBonus\s+Episode|\bSpecial\s+Episode|\bOVA\b|\bMovie\b|\bComplete\s+Pack)/i);
      if (seasonMatch) {
        season = seasonMatch[1].trim();
        const lowerSeason = season.toLowerCase();
        if (lowerSeason === 'ova') {
          season = 'OVA';
        } else if (lowerSeason === 'movie') {
          season = 'Feature Movie';
        } else {
          // Capitalize first letter of each word neatly
          season = season.replace(/\b\w/g, c => c.toUpperCase());
        }
        title = title.replace(seasonMatch[0], '').trim();
      }

      // 3. Resolution extraction: e.g. "480p", "720p", "1080p", "2160p"
      let resolution = '';
      const resMatch = title.match(/(\d+p|4k|2160p)/i);
      if (resMatch) {
        resolution = resMatch[1];
        title = title.replace(resMatch[0], '').trim();
      }

      // 4. Language extraction: e.g. (Hindi-English) or [Multi-Audio]
      let lang = '';
      const allParenthesized = [...title.matchAll(/[\[\(]([a-zA-Z0-9\s-]+)[\]\)]/gi)];
      for (const match of allParenthesized) {
        const potentialLang = match[1].trim();
        // Skip if it's just a 4-digit year (e.g. 2009, 1995, 2024)
        if (/^\d{4}$/.test(potentialLang)) {
          continue;
        }
        lang = potentialLang;
        title = title.replace(match[0], '').trim();
        break;
      }

      // 4b. Fallback: scan for standalone language keywords in the title if no bracketed language found
      if (!lang) {
        const commonLangs = [
          'dual audio', 'multi audio', 'multi-audio', 'single audio',
          'hindi', 'english', 'tamil', 'telugu', 'malayalam', 'kannada',
          'bengali', 'marathi', 'punjabi', 'japanese', 'chinese', 'korean',
          'spanish', 'french'
        ];
        const langRegex = new RegExp(`\\b(${commonLangs.join('|')})\\b`, 'i');
        const standaloneMatch = title.match(langRegex);
        if (standaloneMatch) {
          lang = standaloneMatch[1].trim();
          // Normalize capitalization (e.g. "hindi" -> "Hindi", "multi-audio" -> "Multi-Audio")
          lang = lang.replace(/\b\w/g, c => c.toUpperCase());
          title = title.replace(standaloneMatch[0], '').trim();
        }
      }

      // 4c. Fallback to page-level metadata language if no language resolved yet
      if (!lang && metadata && metadata.language) {
        lang = metadata.language;
      }

      // 5. Split remaining tags
      const tags = title.split(/\s+/)
        .map(s => s.trim())
        .filter(s => s && s.toLowerCase() !== 'download' && s !== '-' && s !== '•');

      return {
        season: season,
        lang: lang,
        resolution: resolution,
        size: size,
        tags: tags,
        fallbackTitle: originalTitle
      };
    }

    function getShortLang(langName) {
      const lower = langName.toLowerCase().trim();
      if (lower.includes('hindi') || lower === 'hin') return 'Hin';
      if (lower.includes('english') || lower === 'eng') return 'Eng';
      if (lower.includes('japanese') || lower === 'jap') return 'Jap';
      if (lower.includes('tamil') || lower === 'tam') return 'Tam';
      if (lower.includes('telugu') || lower === 'tel') return 'Tel';
      if (lower.includes('malayalam') || lower === 'mal') return 'Mal';
      if (lower.includes('kannada') || lower === 'kan') return 'Kan';
      if (lower.includes('bengali') || lower === 'ben') return 'Ben';
      if (lower.includes('marathi') || lower === 'mar') return 'Mar';
      if (lower.includes('punjabi') || lower === 'pun') return 'Pun';
      if (lower.includes('chinese') || lower === 'chi') return 'Chi';
      if (lower.includes('korean') || lower === 'kor') return 'Kor';
      if (lower.includes('spanish') || lower === 'spa') return 'Spa';
      if (lower.includes('french') || lower === 'fre') return 'Fre';
      if (lower.includes('dual') || lower.includes('multi')) return 'Multi';
      return langName.slice(0, 3).replace(/^\w/, c => c.toUpperCase());
    }

    function parseLanguagesAndSubs(qualityTitle, metadata) {
      const audios = new Set();
      const subs = new Set();

      const commonLangs = [
        'hindi', 'english', 'japanese', 'tamil', 'telugu', 'malayalam',
        'kannada', 'bengali', 'marathi', 'punjabi', 'chinese', 'korean',
        'spanish', 'french'
      ];

      // 1. Scan metadata language & subtitles
      if (metadata) {
        if (metadata.language) {
          const mLang = metadata.language.toLowerCase();
          commonLangs.forEach(lang => {
            if (mLang.includes(lang)) {
              audios.add(getShortLang(lang));
            }
          });
          if (mLang.includes('dual') || mLang.includes('multi')) {
            if (audios.size === 0) audios.add('Multi');
          }
        }
        
        // Subtitles from metadata
        const mSub = (metadata.subtitles || metadata.subtitle || '').toLowerCase();
        if (mSub) {
          if (mSub.includes('yes') || mSub.includes('english') || mSub.includes('eng')) {
            subs.add('Eng');
          }
          commonLangs.forEach(lang => {
            if (mSub.includes(lang) && lang !== 'english') {
              subs.add(getShortLang(lang));
            }
          });
        }
      }

      // 2. Scan qualityTitle for Audios
      const titleLower = qualityTitle.toLowerCase();
      commonLangs.forEach(lang => {
        if (titleLower.includes(lang)) {
          audios.add(getShortLang(lang));
        }
      });
      if (titleLower.includes('dual audio') || titleLower.includes('multi audio') || titleLower.includes('multi-audio')) {
        if (audios.size === 0) audios.add('Multi');
      }

      // 3. Scan qualityTitle for Subtitles (e.g. esub, msub, hsub, english subtitles, esubs, msubs)
      if (titleLower.includes('esub') || titleLower.includes('esubs') || titleLower.includes('english sub')) {
        subs.add('Eng');
      }
      if (titleLower.includes('msub') || titleLower.includes('msubs') || titleLower.includes('multi sub') || titleLower.includes('multi-sub')) {
        subs.add('Multi');
      }
      if (titleLower.includes('hsub') || titleLower.includes('hsubs') || titleLower.includes('hindi sub')) {
        subs.add('Hin');
      }

      // Fallbacks if nothing is matched but there is generic info
      if (audios.size === 0) {
        const parsed = parseQualityTitle(qualityTitle, metadata);
        if (parsed.lang) {
          audios.add(getShortLang(parsed.lang));
        } else {
          audios.add('Hin'); // Default fallback
        }
      }

      return {
        audios: Array.from(audios),
        subs: Array.from(subs)
      };
    }

    function getResolutionClass(res) {
      res = (res || '').toLowerCase();
      if (res.includes('480')) return 'res-480p';
      if (res.includes('720')) return 'res-720p';
      if (res.includes('1080')) return 'res-1080p';
      if (res.includes('2160') || res.includes('4k')) return 'res-4k';
      return '';
    }

    function getQualityTheme(res, qualityTitle) {
      const title = (qualityTitle || '').toLowerCase();
      res = (res || '').toLowerCase();
      
      if (res.includes('480')) {
        return {
          class: 'theme-480p',
          title: '480P',
          subtitle: 'Standard Definition',
          color: '#06b6d4',
          bgGlow: 'rgba(6, 182, 212, 0.12)',
          borderGlow: 'rgba(6, 182, 212, 0.35)',
          btnBg: 'rgba(6, 182, 212, 0.15)',
          btnBorder: 'rgba(6, 182, 212, 0.35)',
          btnColor: '#22d3ee',
          btnHoverBg: '#0891b2'
        };
      }
      
      if (res.includes('720')) {
        if (title.includes('265') || title.includes('hevc') || title.includes('10bit')) {
          return {
            class: 'theme-720p-ready',
            title: '720P',
            subtitle: 'HD Ready',
            color: '#10b981',
            bgGlow: 'rgba(16, 185, 129, 0.12)',
            borderGlow: 'rgba(16, 185, 129, 0.35)',
            btnBg: 'rgba(16, 185, 129, 0.15)',
            btnBorder: 'rgba(16, 185, 129, 0.35)',
            btnColor: '#34d399',
            btnHoverBg: '#059669'
          };
        } else {
          return {
            class: 'theme-720p-quality',
            title: '720P',
            subtitle: 'HD Quality',
            color: '#fbbf24',
            bgGlow: 'rgba(245, 158, 11, 0.12)',
            borderGlow: 'rgba(245, 158, 11, 0.35)',
            btnBg: 'rgba(245, 158, 11, 0.15)',
            btnBorder: 'rgba(245, 158, 11, 0.35)',
            btnColor: '#fbbf24',
            btnHoverBg: '#d97706'
          };
        }
      }
      
      if (res.includes('1080')) {
        return {
          class: 'theme-1080p',
          title: '1080P',
          subtitle: 'Full HD',
          color: '#a855f7',
          bgGlow: 'rgba(168, 85, 247, 0.12)',
          borderGlow: 'rgba(168, 85, 247, 0.35)',
          btnBg: 'rgba(168, 85, 247, 0.15)',
          btnBorder: 'rgba(168, 85, 247, 0.35)',
          btnColor: '#c084fc',
          btnHoverBg: '#9333ea'
        };
      }
      
      if (res.includes('2160') || res.includes('4k')) {
        return {
          class: 'theme-4k',
          title: '4K UHD',
          subtitle: 'Ultra HD',
          color: '#ec4899',
          bgGlow: 'rgba(236, 72, 153, 0.12)',
          borderGlow: 'rgba(236, 72, 153, 0.35)',
          btnBg: 'rgba(236, 72, 153, 0.15)',
          btnBorder: 'rgba(236, 72, 153, 0.35)',
          btnColor: '#f472b6',
          btnHoverBg: '#db2777'
        };
      }
      
      // Fallback
      return {
        class: 'theme-default',
        title: res.toUpperCase() || 'VIDEO',
        subtitle: 'High Quality',
        color: '#94a3b8',
        bgGlow: 'rgba(148, 163, 184, 0.12)',
        borderGlow: 'rgba(148, 163, 184, 0.35)',
        btnBg: 'rgba(148, 163, 184, 0.15)',
        btnBorder: 'rgba(148, 163, 184, 0.35)',
        btnColor: '#cbd5e1',
        btnHoverBg: '#475569'
      };
    }

    // Same-Page Options Detail view
    function viewDetails(encodedItem) {
      const item = JSON.parse(decodeURIComponent(encodedItem));
      const resultsDiv = document.getElementById('search-results');
      const detailPanel = document.getElementById('detail-panel');
      const optionList = document.getElementById('option-list');
      const metaRow = document.getElementById('detail-meta-row');
      
      // Hide search bar, category buttons, tabs bar, and search card grid
      document.querySelector('.search-bar-row').style.display = 'none';
      document.querySelector('.categories-row').style.display = 'none';
      document.querySelector('.tabs-container').style.display = 'none';
      resultsDiv.style.display = 'none';
      
      detailPanel.style.display = 'flex';
      
      // Load movie poster into page background fading in smoothly!
      const bgElement = document.getElementById('details-page-bg');
      const bgImg = document.getElementById('details-page-bg-img');
      if (item.thumbnail) {
        bgImg.src = `/api/thumbnail?url=${encodeURIComponent(item.thumbnail)}`;
        bgElement.style.opacity = '0.35';
      } else {
        bgImg.src = '';
        bgElement.style.opacity = '0';
      }

      document.getElementById('detail-title').innerText = item.title;
      if (metaRow) metaRow.style.display = 'none';
      
      // Render loader inside details list
      optionList.innerHTML = Array.from({length: 3}).map(() => `
        <div class="option-group-card skeleton-card" style="min-height: 80px; margin-bottom:14px;">
          <div style="display:flex; gap:12px; width:40%;">
            <div class="skeleton-text" style="width: 60px; height: 24px; border-radius:12px;"></div>
            <div class="skeleton-text" style="width: 80px; height: 24px; border-radius:12px;"></div>
          </div>
          <div style="display:flex; gap:12px; margin-left:auto;">
            <div class="skeleton-text" style="width: 120px; height: 36px; border-radius:8px;"></div>
          </div>
        </div>
      `).join('');

      fetch(`/api/detail?url=${encodeURIComponent(item.url)}`)
        .then(r => r.json())
        .then(data => {
          if (data.error) {
            optionList.innerHTML = `<div style="text-align: center; color: var(--crimson); padding: 40px;">Error: ${data.error}</div>`;
            return;
          }
          if (data.options.length === 0) {
            optionList.innerHTML = '<div style="text-align: center; color: var(--text-dim); padding: 40px;">No download options/qualities found on page.</div>';
            return;
          }
          
          // Extract global languages and tags across all options
          const allLangs = new Set();
          const allGlobalTags = new Set();
          data.options.forEach(opt => {
            const parsed = parseQualityTitle(opt.quality, data.metadata);
            if (parsed.lang) allLangs.add(parsed.lang);
            parsed.tags.forEach(tag => {
              const t = tag.toLowerCase();
              if (t.includes('sub') || t.includes('audio') || t === 'dual' || t.includes('multi')) {
                allGlobalTags.add(tag);
              }
            });
          });

          // Hide global meta row — tags now live inside each season accordion
          if (metaRow) metaRow.style.display = 'none';

          // 1. Group options by exact "quality" text first, separating duplicates into separate rows
          const qualityGroups = {};
          data.options.forEach(opt => {
            const baseKey = opt.quality.trim();
            let key = baseKey;
            let counter = 1;

            // Normalize button text to check for duplicates (e.g. "episode", "batch", "zip", etc.)
            const btnTextLower = opt.button_text.toLowerCase();
            const getBtnType = (txt) => {
              if (txt.includes('zip') || txt.includes('batch') || txt.includes('pack')) return 'batch';
              if (txt.includes('telegram')) return 'telegram';
              return 'episode';
            };
            const btnType = getBtnType(btnTextLower);

            // Find an existing group under this baseKey that does NOT already have this button type
            while (qualityGroups[key] && qualityGroups[key].some(existingOpt => getBtnType(existingOpt.button_text.toLowerCase()) === btnType)) {
              counter++;
              key = `${baseKey} ##__DUP__## ${counter}`;
            }

            if (!qualityGroups[key]) {
              qualityGroups[key] = [];
            }
            qualityGroups[key].push(opt);
          });

          // 2. Now group these quality groups by their parsed "Season"
          const seasonGroups = {};
          Object.entries(qualityGroups).forEach(([quality, opts]) => {
            const cleanQuality = quality.split(' ##__DUP__## ')[0];
            const parsed = parseQualityTitle(cleanQuality, data.metadata);
            // Default season label if none parsed (e.g. for movies)
            const seasonName = parsed.season || "Complete Pack / Options";
            if (!seasonGroups[seasonName]) {
              seasonGroups[seasonName] = [];
            }
            seasonGroups[seasonName].push({
              quality: quality,
              parsed: parsed,
              opts: opts
            });
          });

          // 3. Sort season names nicely (e.g. Season 1 before Season 2)
          const entries = Object.entries(seasonGroups);
          entries.sort((a, b) => {
            const numA = parseInt(a[0].match(/\d+/)) || 0;
            const numB = parseInt(b[0].match(/\d+/)) || 0;
            if (numA && numB) return numA - numB;
            return a[0].localeCompare(b[0]);
          });

          // 4. Build accordion HTML structure (auto-expanded if there is only 1 item)
          let accordionHtml = `<div class="accordion-list">`;
          
          entries.forEach(([seasonName, items], index) => {
            const isSingleItem = entries.length === 1;
            const activeClass = isSingleItem ? 'active' : '';
            const styleHeight = isSingleItem ? 'style="max-height: none;"' : 'style="max-height: 0;"';

            // Render options inside this Season group
            const cardsInnerHtml = items.map(item => {
              const parsed = item.parsed;
              const theme = getQualityTheme(parsed.resolution, item.quality);
              
              // Get clean parsed audios and subtitles
              const mediaInfo = parseLanguagesAndSubs(item.quality, data.metadata);

              // Build buttons row (side-by-side)
              const buttonsHtml = item.opts.map(opt => {
                let icon = '⚡';
                let btnClass = 'primary-dl-btn';
                const txt = opt.button_text.toLowerCase();
                
                if (txt.includes('zip') || txt.includes('batch') || txt.includes('pack')) {
                  icon = '📦';
                  btnClass = 'secondary-dl-btn';
                } else if (txt.includes('telegram')) {
                  icon = '✈️';
                  btnClass = 'secondary-dl-btn';
                }
                
                // For primary button, apply custom HSL glow and matching border inline
                let inlineStyle = '';
                let hoverAttributes = '';
                if (btnClass === 'primary-dl-btn') {
                  inlineStyle = `background: ${theme.btnBg}; border: 1px solid ${theme.btnBorder}; color: ${theme.btnColor}; box-shadow: 0 2px 10px ${theme.btnBg};`;
                  hoverAttributes = `onmouseover="this.style.background='${theme.btnHoverBg}'; this.style.color='#ffffff'; this.style.box-shadow='0 4px 15px ${theme.btnBorder}'" onmouseout="this.style.background='${theme.btnBg}'; this.style.color='${theme.btnColor}'; this.style.box-shadow='0 2px 10px ${theme.btnBg}'"`;
                }

                return `
                  <button class="option-dl-btn ${btnClass}" style="${inlineStyle}" ${hoverAttributes} onclick="startDownload('${encodeURIComponent(opt.url)}')">
                    <span class="btn-icon">${icon}</span>
                    ${opt.button_text}
                  </button>
                `;
              }).join('');

              // Monitor icon element
              const monitorSvg = `
                <svg class="monitor-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
                  <line x1="8" y1="21" x2="16" y2="21"></line>
                  <line x1="12" y1="17" x2="12" y2="21"></line>
                </svg>
              `;

              return `
                <div class="option-group-card ${theme.class}">
                  <div class="option-left-block">
                    <div class="monitor-icon-wrapper">
                      ${monitorSvg}
                    </div>
                    <div class="resolution-info">
                      <div class="res-title">${theme.title}</div>
                      <div class="res-subtitle">${theme.subtitle}</div>
                    </div>
                  </div>
                  
                  <div class="option-middle-block">
                    <span class="pill-size">${parsed.size || 'N/A'}</span>
                    ${(() => {
                      const cleanQuality = item.quality.split(' ##__DUP__## ')[0];
                      const isDup = item.quality.includes('##__DUP__##');
                      const dupMatch = item.quality.match(/##__DUP__##\s*(\d+)/);
                      const dupNum = dupMatch ? parseInt(dupMatch[1]) : 1;

                      // Check if the movie title implies Colour and B&W versions
                      const titleLower = document.getElementById('detail-title').innerText.toLowerCase();
                      const hasColourAndBW = (titleLower.includes('colour') || titleLower.includes('color')) && 
                                             (titleLower.includes('b&w') || titleLower.includes('bw') || titleLower.includes('black and white'));

                      if (hasColourAndBW) {
                        if (!isDup) {
                          return `<span class="pill-size" style="background: rgba(168, 85, 247, 0.1); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.25); margin-left: 8px;">COLOUR Version</span>`;
                        } else if (dupNum === 2) {
                          return `<span class="pill-size" style="background: rgba(148, 163, 184, 0.1); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.25); margin-left: 8px;">B&W Version</span>`;
                        }
                      }
                      
                      // Fallback to Set 1 / Set 2 if it's just a general duplicate
                      if (isDup) {
                        return `<span class="pill-size" style="background: rgba(255, 255, 255, 0.05); color: var(--text-sub); border: 1px solid rgba(255, 255, 255, 0.1); margin-left: 8px;">Set ${dupNum}</span>`;
                      } else {
                        // Check if any other quality has a duplicate. If so, label this as Set 1
                        const hasAnyDupForThisBase = Object.keys(qualityGroups).some(k => k.startsWith(cleanQuality) && k.includes('##__DUP__##'));
                        if (hasAnyDupForThisBase) {
                          return `<span class="pill-size" style="background: rgba(255, 255, 255, 0.05); color: var(--text-sub); border: 1px solid rgba(255, 255, 255, 0.1); margin-left: 8px;">Set 1</span>`;
                        }
                      }
                      return '';
                    })()}
                    ${parsed.tags.map(tag => {
                      const t = tag.toLowerCase();
                      if (t.includes('10bit') || t.includes('x264') || t.includes('x265') || t.includes('hevc')) {
                        let badgeStyle = 'background: rgba(255, 255, 255, 0.05); color: var(--text-sub); border: 1px solid rgba(255, 255, 255, 0.1); margin-left: 8px;';
                        if (t.includes('10bit')) {
                          badgeStyle = 'background: rgba(16, 185, 129, 0.1); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25); margin-left: 8px;';
                        } else if (t.includes('x264')) {
                          badgeStyle = 'background: rgba(245, 158, 11, 0.1); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.25); margin-left: 8px;';
                        } else if (t.includes('hevc') || t.includes('x265')) {
                          badgeStyle = 'background: rgba(168, 85, 247, 0.1); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.25); margin-left: 8px;';
                        }
                        return `<span class="pill-size" style="${badgeStyle}">${tag}</span>`;
                      }
                      return '';
                    }).join('')}
                  </div>

                  <div class="divider-line"></div>

                  <div class="option-buttons-row">
                    ${buttonsHtml}
                  </div>
                </div>
              `;
            }).join('');

            const countText = items.length === 1 ? '1 Quality Option' : `${items.length} Quality Options`;

            // Compute per-season language and subtitle pills
            const seasonAudios = new Set();
            const seasonSubs = new Set();
            items.forEach(item => {
              const mediaInfo = parseLanguagesAndSubs(item.quality, data.metadata);
              mediaInfo.audios.forEach(aud => seasonAudios.add(aud));
              mediaInfo.subs.forEach(sub => seasonSubs.add(sub));
            });

            let seasonPillsHtml = '';
            if (seasonAudios.size > 0) {
              const audStr = Array.from(seasonAudios).join('-');
              seasonPillsHtml += `<span class="pill-badge pill-lang" style="font-size: 9.5px; padding: 3px 10px; margin-left: 6px;">🔊 ${audStr}</span>`;
            }
            if (seasonSubs.size > 0) {
              const subStr = Array.from(seasonSubs).join('-');
              seasonPillsHtml += `<span class="pill-badge pill-tag" style="font-size: 9.5px; padding: 3px 10px; margin-left: 4px; background: rgba(59, 130, 246, 0.1); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.25);">📝 ${subStr}</span>`;
            }

            accordionHtml += `
              <div class="accordion-item ${activeClass}">
                <div class="accordion-header" onclick="toggleAccordion(this)">
                  <div class="accordion-header-left">
                    <span class="accordion-icon">🍿</span>
                    <span class="accordion-title">${seasonName}</span>
                    <span class="accordion-count">${countText}</span>
                    ${seasonPillsHtml}
                  </div>
                  <svg class="chevron-icon" width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" style="stroke-width:2.5;"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"></path></svg>
                </div>
                <div class="accordion-content" ${styleHeight}>
                  <div class="accordion-content-inner">
                    ${cardsInnerHtml}
                  </div>
                </div>
              </div>
            `;
          });

          accordionHtml += `</div>`;
          optionList.innerHTML = accordionHtml;
        })
        .catch(e => {
          optionList.innerHTML = `<div style="text-align: center; color: var(--crimson); padding: 40px;">Error: ${e.message}</div>`;
        });
    }

    function toggleAccordion(header) {
      const item = header.parentElement;
      const content = item.querySelector('.accordion-content');
      const isActive = item.classList.contains('active');
      
      // Close all other accordions smoothly
      document.querySelectorAll('.accordion-item').forEach(otherItem => {
        if (otherItem !== item) {
          otherItem.classList.remove('active');
          otherItem.querySelector('.accordion-content').style.maxHeight = null;
        }
      });

      if (isActive) {
        item.classList.remove('active');
        content.style.maxHeight = null;
      } else {
        item.classList.add('active');
        content.style.maxHeight = content.scrollHeight + 'px';
        // Scroll the opened accordion into view smoothly after the expansion animation
        setTimeout(() => {
          item.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 320);
      }
    }

    function closeDetails() {
      // Fade out background poster
      const bgElement = document.getElementById('details-page-bg');
      if (bgElement) bgElement.style.opacity = '0';
      
      // Restore search bar, categories, and tabs bar visibility
      document.querySelector('.search-bar-row').style.display = 'block';
      document.querySelector('.categories-row').style.display = 'flex';
      document.querySelector('.tabs-container').style.display = 'flex';
      
      document.getElementById('detail-panel').style.display = 'none';
      const q = document.getElementById('search-box').value.trim();
      document.getElementById('search-results').style.display = q.length < 2 ? 'block' : 'grid';
    }

    function showDirectFromDetails() {
      activeTab = 'direct';
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-panel').forEach(panel => panel.classList.remove('active'));
      
      document.querySelector('.tab-btn:nth-child(2)').classList.add('active');
      document.getElementById('direct-view').classList.add('active');
      
      // Hide the top header navigation tabs & direct input box
      document.querySelector('.tabs-container').style.display = 'none';
      document.querySelector('.direct-input-row').style.display = 'none';
      
      // Show the premium back button
      document.getElementById('direct-back-row').style.display = 'block';
    }

    function goBackToMovie() {
      // Switch back to search tab under-the-hood
      activeTab = 'search';
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.view-panel').forEach(panel => panel.classList.remove('active'));
      
      document.querySelector('.tab-btn:nth-child(1)').classList.add('active');
      document.getElementById('search-view').classList.add('active');
      
      // Hide the back button, restore direct input row
      document.getElementById('direct-back-row').style.display = 'none';
      document.querySelector('.direct-input-row').style.display = 'flex';
      
      // Keep tabs container hidden since the user is in details view
      document.querySelector('.tabs-container').style.display = 'none';
    }

    // Download API communication handlers
    function startDownload(url) {
      url = decodeURIComponent(url);
      isResolvingNewDownload = true;

      // Clear the downloads list UI immediately with beautiful skeleton placeholders so they don't see old downloads
      const list = document.getElementById('downloads-list');
      if (list) {
        list.innerHTML = Array.from({length: 3}).map(() => `
          <div class="download-card skeleton-card" style="min-height: 70px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; padding: 14px 20px;">
            <div style="display: flex; align-items: center; gap: 12px; width: 60%;">
              <div class="skeleton-text" style="width: 24px; height: 16px; border-radius: 4px;"></div>
              <div class="skeleton-text" style="width: 80%; height: 16px; border-radius: 4px;"></div>
            </div>
            <div class="skeleton-text" style="width: 140px; height: 32px; border-radius: 20px;"></div>
          </div>
        `).join('');
      }

      if (isCloudMode) {
        // Skip folder chooser in cloud mode
        showDirectFromDetails();
        fetch('/api/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: url, output_dir: 'cloud' })
        });
        return;
      }

      fetch('/api/choose-folder', { method: 'POST' })
        .then(r => r.json())
        .then(folderData => {
          if (folderData.cancelled || !folderData.path) {
            isResolvingNewDownload = false;
            alert("Download cancelled: No directory selected.");
            pollDownloads();
            return;
          }
          
          // Switch to Direct URL tab immediately to watch downloads progress
          showDirectFromDetails();
          
          fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, output_dir: folderData.path })
          });
        });
    }

    function startDirectDownload() {
      const url = document.getElementById('direct-url-box').value.trim();
      if (!url) return;
      document.getElementById('direct-url-box').value = '';
      startDownload(encodeURIComponent(url));
    }

    // Polling Downloads status
    function pollDownloads() {
      if (activeTab !== 'direct') return; // only poll active view
      
      fetch('/api/downloads')
        .then(r => r.json())
        .then(data => {
          const list = document.getElementById('downloads-list');
          if (data.downloads.length === 0) {
            if (isResolvingNewDownload) {
              // Keep displaying the skeleton loader while the server is resolving
              return;
            }
            list.innerHTML = '';
            return;
          }

          // Reset the resolving flag once cards are successfully fetched
          isResolvingNewDownload = false;

          let hasFailed = false;
          list.innerHTML = data.downloads.map(dl => {
            if (dl.state === 3) hasFailed = true; // State Failed
            
            let statusClass = '';
            if (dl.state === 2) statusClass = 'done';
            if (dl.state === 3) statusClass = 'failed';
            if (dl.state === 1) statusClass = 'active';

            const methodClass = (dl.method || '').toLowerCase();

            // Right side content depends on state
            let rightContent = '';
            if (dl.resolved_url) {
              // Completed with resolved URL — show download button
              if (dl.size) {
                rightContent = `
                  <a href="${dl.resolved_url}" download="${dl.filename}" target="_blank" class="dl-download-btn-partitioned">
                    <span class="dl-btn-left">☁ Download to Device</span>
                    <span class="dl-btn-right">(${dl.size})</span>
                  </a>
                `;
              } else {
                rightContent = `<a href="${dl.resolved_url}" download="${dl.filename}" target="_blank" class="dl-download-btn">☁ Download to Device</a>`;
              }
            } else if (dl.state === 3) {
              // Failed — show retry button
              rightContent = `<button class="dl-retry-btn" onclick="retryDownload(${dl.index})">Retry</button>`;
            } else if (dl.state === 1) {
              // Actively downloading — show compact status
              rightContent = `<span class="dl-status-compact" style="color: var(--blue)">${dl.status}</span>`;
            } else {
              // Queued or other — show status text
              rightContent = `<span class="dl-status-compact" style="color: var(--text-sub)">${dl.status}</span>`;
            }

            // Only show progress bar if not yet completed
            const progressBar = (dl.state !== 2) ? `<div class="dl-progress-mini" style="width: ${dl.progress * 100}%"></div>` : '';

            return `
              <div class="download-card ${statusClass}">
                <div class="download-card-left">
                  <span class="dl-index">#${dl.index}</span>
                  <span class="dl-filename" title="${dl.filename}">${dl.filename}</span>
                  ${dl.method ? `<span class="method-badge ${methodClass}">${dl.method}</span>` : ''}
                </div>
                <div class="download-card-right">
                  ${rightContent}
                </div>
                ${progressBar}
              </div>
            `;
          }).join('');

          // Show retry all failures button if failures exist
          document.getElementById('retry-failed-btn').style.display = hasFailed ? 'block' : 'none';
        })
        .catch(console.error);
    }

    function pollStatus() {
      fetch('/api/status')
        .then(r => r.json())
        .then(data => {
          isCloudMode = !!data.cloud_mode;
          
          document.getElementById('footer-status-text').innerHTML = 
            `Downloads: Active ${data.active_threads} — Completed ${data.done_count}/${data.total_count} failed ${data.fail_count}`;
          
          const tgText = document.getElementById('tg-text');
          const tgDot = document.getElementById('tg-dot');
          tgText.innerText = data.telegram.text;
          
          tgDot.className = 'tg-dot';
          if (data.telegram.color === 'green') tgDot.classList.add('ready');
          else if (data.telegram.color === 'amber') tgDot.classList.add('warning');
          else tgDot.classList.add('notready');
        })
        .catch(console.error);
    }

    function retryDownload(index) {
      fetch('/api/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index: index })
      });
    }

    function retryFailedAll() {
      fetch('/api/retry-all', { method: 'POST' });
    }

    // --- Premium Diagnostics & Cache Management Actions ---
    window.openDiagnostics = function() {
      const modal = document.getElementById('diagnostics-modal');
      if (!modal) return;
      modal.style.display = 'flex';
      
      // 1. Fetch server stats
      fetch('/api/storage-stats')
        .then(res => res.json())
        .then(data => {
          document.getElementById('diag-project-size').innerText = data.total_project_size;
          document.getElementById('diag-server-suggest').innerText = `${data.imdb_suggest_cache_size} (${data.imdb_suggest_cache_count} queries)`;
          document.getElementById('diag-server-posters').innerText = `${data.cached_posters_size} (${data.cached_posters_count} posters)`;
          document.getElementById('diag-trending-cache').innerText = data.trending_cache_size;
          document.getElementById('diag-server-time').innerText = data.server_time;
        })
        .catch(err => {
          console.error(err);
          const fields = ['diag-project-size', 'diag-server-suggest', 'diag-server-posters', 'diag-trending-cache', 'diag-server-time'];
          fields.forEach(f => {
            const el = document.getElementById(f);
            if (el) el.innerText = 'Error loading';
          });
        });

      // 2. Fetch client stats
      try {
        const clientSuggestCount = Object.keys(suggestionsClientCache).length;
        const serialized = JSON.stringify(suggestionsClientCache);
        const clientSuggestBytes = new TextEncoder().encode(serialized).length;
        
        let clientSuggestSize = `${(clientSuggestBytes / 1024).toFixed(2)} KB`;
        if (clientSuggestBytes >= 1048576) {
          clientSuggestSize = `${(clientSuggestBytes / 1048576).toFixed(2)} MB`;
        }

        // Calculate total localStorage size
        let totalLocalStorageBytes = 0;
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          const val = localStorage.getItem(key);
          totalLocalStorageBytes += new TextEncoder().encode(key + val).length;
        }
        let totalLocalSize = `${(totalLocalStorageBytes / 1024).toFixed(2)} KB`;
        if (totalLocalStorageBytes >= 1048576) {
          totalLocalSize = `${(totalLocalStorageBytes / 1048576).toFixed(2)} MB`;
        }

        document.getElementById('diag-client-suggest').innerText = `${clientSuggestSize} (${clientSuggestCount} queries)`;
        document.getElementById('diag-client-size').innerText = totalLocalSize;
      } catch (e) {
        document.getElementById('diag-client-suggest').innerText = '0 B (0 queries)';
        document.getElementById('diag-client-size').innerText = '0 B';
      }
    };

    window.closeDiagnostics = function() {
      const modal = document.getElementById('diagnostics-modal');
      if (modal) modal.style.display = 'none';
    };

    window.clearServerCache = function() {
      if (!confirm("Are you sure you want to permanently clear the server-side suggestions cache and delete all cached posters from the SSD?")) return;
      
      const serverBtn = document.querySelector('.server-btn');
      if (serverBtn) {
        serverBtn.innerText = "🧹 Clearing Server SSD...";
        serverBtn.disabled = true;
      }

      fetch('/api/clear-server-cache')
        .then(res => res.json())
        .then(data => {
          alert(data.message || "Server cache cleared successfully!");
          window.openDiagnostics(); // reload stats
        })
        .catch(err => {
          alert("Failed to clear server cache: " + err);
        })
        .finally(() => {
          if (serverBtn) {
            serverBtn.innerText = "🧹 Clear Server SSD Cache";
            serverBtn.disabled = false;
          }
        });
    };

    window.clearClientCache = function() {
      if (!confirm("Are you sure you want to delete all client-side browser suggestion caches?")) return;
      
      try {
        localStorage.removeItem(IMDB_SUGGESTIONS_CACHE_KEY);
        suggestionsClientCache = {};
        alert("Client-side suggestions cache cleared successfully!");
        window.openDiagnostics(); // reload stats
      } catch (e) {
        alert("Failed to clear client cache: " + e);
      }
    };
