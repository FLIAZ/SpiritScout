// \0rolldown/runtime.js
var e = Object.create, t = Object.defineProperty, n = Object.getOwnPropertyDescriptor, r = Object.getOwnPropertyNames, i = Object.getPrototypeOf, a = Object.prototype.hasOwnProperty, o = (e, t) => () => (t || (e((t = { exports: {} }).exports, t, t.exports), e = null), t.exports), s = (e, i, o, s) => {
	if (i && typeof i == "object" || typeof i == "function") for (var c = r(i), l = 0, u = c.length, d; l < u; l++) d = c[l], !a.call(e, d) && d !== o && t(e, d, {
		get: ((e) => i[e]).bind(null, d),
		enumerable: !(s = n(i, d)) || s.enumerable
	});
	return e;
}, c = (n, r, o) => (o = n == null ? {} : e(i(n)), s(r || !n || !n.__esModule || !a.call(n, "default") ? t(o, "default", {
	value: n,
	enumerable: !0
}) : o, n)), l = /* @__PURE__ */ o(((e) => {
	var t = Symbol.for("react.transitional.element"), n = Symbol.for("react.portal"), r = Symbol.for("react.fragment"), i = Symbol.for("react.strict_mode"), a = Symbol.for("react.profiler"), o = Symbol.for("react.consumer"), s = Symbol.for("react.context"), c = Symbol.for("react.forward_ref"), l = Symbol.for("react.suspense"), u = Symbol.for("react.memo"), d = Symbol.for("react.lazy"), f = Symbol.for("react.activity"), p = Symbol.for("react.view_transition"), m = Symbol.iterator;
	function h(e) {
		return typeof e != "object" || !e ? null : (e = m && e[m] || e["@@iterator"], typeof e == "function" ? e : null);
	}
	var g = {
		isMounted: function() {
			return !1;
		},
		enqueueForceUpdate: function() {},
		enqueueReplaceState: function() {},
		enqueueSetState: function() {}
	}, _ = Object.assign, v = {};
	function y(e, t, n) {
		this.props = e, this.context = t, this.refs = v, this.updater = n || g;
	}
	y.prototype.isReactComponent = {}, y.prototype.setState = function(e, t) {
		if (typeof e != "object" && typeof e != "function" && e != null) throw Error("takes an object of state variables to update or a function which returns an object of state variables.");
		this.updater.enqueueSetState(this, e, t, "setState");
	}, y.prototype.forceUpdate = function(e) {
		this.updater.enqueueForceUpdate(this, e, "forceUpdate");
	};
	function b() {}
	b.prototype = y.prototype;
	function x(e, t, n) {
		this.props = e, this.context = t, this.refs = v, this.updater = n || g;
	}
	var S = x.prototype = new b();
	S.constructor = x, _(S, y.prototype), S.isPureReactComponent = !0;
	var C = Array.isArray;
	function w() {}
	var T = {
		H: null,
		A: null,
		T: null,
		S: null
	}, ee = Object.prototype.hasOwnProperty;
	function E(e, n, r) {
		var i = r.ref;
		return {
			$$typeof: t,
			type: e,
			key: n,
			ref: i === void 0 ? null : i,
			props: r
		};
	}
	function te(e, t) {
		return E(e.type, t, e.props);
	}
	function ne(e) {
		return typeof e == "object" && !!e && e.$$typeof === t;
	}
	function re(e) {
		var t = {
			"=": "=0",
			":": "=2"
		};
		return "$" + e.replace(/[=:]/g, function(e) {
			return t[e];
		});
	}
	var D = /\/+/g;
	function ie(e, t) {
		return typeof e == "object" && e && e.key != null ? re("" + e.key) : t.toString(36);
	}
	function ae(e) {
		switch (e.status) {
			case "fulfilled": return e.value;
			case "rejected": throw e.reason;
			default: switch (typeof e.status == "string" ? e.then(w, w) : (e.status = "pending", e.then(function(t) {
				e.status === "pending" && (e.status = "fulfilled", e.value = t);
			}, function(t) {
				e.status === "pending" && (e.status = "rejected", e.reason = t);
			})), e.status) {
				case "fulfilled": return e.value;
				case "rejected": throw e.reason;
			}
		}
		throw e;
	}
	function oe(e, r, i, a, o) {
		var s = typeof e;
		(s === "undefined" || s === "boolean") && (e = null);
		var c = !1;
		if (e === null) c = !0;
		else switch (s) {
			case "bigint":
			case "string":
			case "number":
				c = !0;
				break;
			case "object": switch (e.$$typeof) {
				case t:
				case n:
					c = !0;
					break;
				case d: return c = e._init, oe(c(e._payload), r, i, a, o);
			}
		}
		if (c) return o = o(e), c = a === "" ? "." + ie(e, 0) : a, C(o) ? (i = "", c != null && (i = c.replace(D, "$&/") + "/"), oe(o, r, i, "", function(e) {
			return e;
		})) : o != null && (ne(o) && (o = te(o, i + (o.key == null || e && e.key === o.key ? "" : ("" + o.key).replace(D, "$&/") + "/") + c)), r.push(o)), 1;
		c = 0;
		var l = a === "" ? "." : a + ":";
		if (C(e)) for (var u = 0; u < e.length; u++) a = e[u], s = l + ie(a, u), c += oe(a, r, i, s, o);
		else if (u = h(e), typeof u == "function") for (e = u.call(e), u = 0; !(a = e.next()).done;) a = a.value, s = l + ie(a, u++), c += oe(a, r, i, s, o);
		else if (s === "object") {
			if (typeof e.then == "function") return oe(ae(e), r, i, a, o);
			throw r = String(e), Error("Objects are not valid as a React child (found: " + (r === "[object Object]" ? "object with keys {" + Object.keys(e).join(", ") + "}" : r) + "). If you meant to render a collection of children, use an array instead.");
		}
		return c;
	}
	function se(e, t, n) {
		if (e == null) return e;
		var r = [], i = 0;
		return oe(e, r, "", "", function(e) {
			return t.call(n, e, i++);
		}), r;
	}
	function ce(e) {
		if (e._status === -1) {
			var t = e._result, n = t();
			n.then(function(t) {
				(e._status === 0 || e._status === -1) && (e._status = 1, e._result = t, n.status === void 0 && (n.status = "fulfilled", n.value = t));
			}, function(t) {
				(e._status === 0 || e._status === -1) && (e._status = 2, e._result = t, n.status === void 0 && (n.status = "rejected", n.reason = t));
			}), e._status === -1 && (e._status = 0, e._result = n);
		}
		if (e._status === 1) return e._result.default;
		throw e._result;
	}
	var le = typeof reportError == "function" ? reportError : function(e) {
		if (typeof window == "object" && typeof window.ErrorEvent == "function") {
			var t = new window.ErrorEvent("error", {
				bubbles: !0,
				cancelable: !0,
				message: typeof e == "object" && e && typeof e.message == "string" ? String(e.message) : String(e),
				error: e
			});
			if (!window.dispatchEvent(t)) return;
		} else if (typeof process == "object" && typeof process.emit == "function") {
			process.emit("uncaughtException", e);
			return;
		}
		console.error(e);
	};
	function ue(e) {
		var t = T.T, n = {};
		n.types = t === null ? null : t.types, T.T = n;
		try {
			var r = e(), i = T.S;
			i !== null && i(n, r), typeof r == "object" && r && typeof r.then == "function" && r.then(w, le);
		} catch (e) {
			le(e);
		} finally {
			t !== null && n.types !== null && (t.types = n.types), T.T = t;
		}
	}
	function de(e) {
		var t = T.T;
		if (t !== null) {
			var n = t.types;
			n === null ? t.types = [e] : n.indexOf(e) === -1 && n.push(e);
		} else ue(de.bind(null, e));
	}
	var fe = {
		map: se,
		forEach: function(e, t, n) {
			se(e, function() {
				t.apply(this, arguments);
			}, n);
		},
		count: function(e) {
			var t = 0;
			return se(e, function() {
				t++;
			}), t;
		},
		toArray: function(e) {
			return se(e, function(e) {
				return e;
			}) || [];
		},
		only: function(e) {
			if (!ne(e)) throw Error("React.Children.only expected to receive a single React element child.");
			return e;
		}
	};
	e.Activity = f, e.Children = fe, e.Component = y, e.Fragment = r, e.Profiler = a, e.PureComponent = x, e.StrictMode = i, e.Suspense = l, e.ViewTransition = p, e.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE = T, e.__COMPILER_RUNTIME = {
		__proto__: null,
		c: function(e) {
			return T.H.useMemoCache(e);
		}
	}, e.addTransitionType = de, e.cache = function(e) {
		return function() {
			return e.apply(null, arguments);
		};
	}, e.cacheSignal = function() {
		return null;
	}, e.cloneElement = function(e, t, n) {
		if (e == null) throw Error("The argument must be a React element, but you passed " + e + ".");
		var r = _({}, e.props), i = e.key;
		if (t != null) for (a in t.key !== void 0 && (i = "" + t.key), t) !ee.call(t, a) || a === "key" || a === "__self" || a === "__source" || a === "ref" && t.ref === void 0 || (r[a] = t[a]);
		var a = arguments.length - 2;
		if (a === 1) r.children = n;
		else if (1 < a) {
			for (var o = Array(a), s = 0; s < a; s++) o[s] = arguments[s + 2];
			r.children = o;
		}
		return E(e.type, i, r);
	}, e.createContext = function(e) {
		return e = {
			$$typeof: s,
			_currentValue: e,
			_currentValue2: e,
			_threadCount: 0,
			Provider: null,
			Consumer: null
		}, e.Provider = e, e.Consumer = {
			$$typeof: o,
			_context: e
		}, e;
	}, e.createElement = function(e, t, n) {
		var r, i = {}, a = null;
		if (t != null) for (r in t.key !== void 0 && (a = "" + t.key), t) ee.call(t, r) && r !== "key" && r !== "__self" && r !== "__source" && (i[r] = t[r]);
		var o = arguments.length - 2;
		if (o === 1) i.children = n;
		else if (1 < o) {
			for (var s = Array(o), c = 0; c < o; c++) s[c] = arguments[c + 2];
			i.children = s;
		}
		if (e && e.defaultProps) for (r in o = e.defaultProps, o) i[r] === void 0 && (i[r] = o[r]);
		return E(e, a, i);
	}, e.createRef = function() {
		return { current: null };
	}, e.forwardRef = function(e) {
		return {
			$$typeof: c,
			render: e
		};
	}, e.isValidElement = ne, e.lazy = function(e) {
		return {
			$$typeof: d,
			_payload: {
				_status: -1,
				_result: e
			},
			_init: ce
		};
	}, e.memo = function(e, t) {
		return {
			$$typeof: u,
			type: e,
			compare: t === void 0 ? null : t
		};
	}, e.startTransition = ue, e.unstable_useCacheRefresh = function() {
		return T.H.useCacheRefresh();
	}, e.use = function(e) {
		return T.H.use(e);
	}, e.useActionState = function(e, t, n) {
		return T.H.useActionState(e, t, n);
	}, e.useCallback = function(e, t) {
		return T.H.useCallback(e, t);
	}, e.useContext = function(e) {
		return T.H.useContext(e);
	}, e.useDebugValue = function() {}, e.useDeferredValue = function(e, t) {
		return T.H.useDeferredValue(e, t);
	}, e.useEffect = function(e, t) {
		return T.H.useEffect(e, t);
	}, e.useEffectEvent = function(e) {
		return T.H.useEffectEvent(e);
	}, e.useId = function() {
		return T.H.useId();
	}, e.useImperativeHandle = function(e, t, n) {
		return T.H.useImperativeHandle(e, t, n);
	}, e.useInsertionEffect = function(e, t) {
		return T.H.useInsertionEffect(e, t);
	}, e.useLayoutEffect = function(e, t) {
		return T.H.useLayoutEffect(e, t);
	}, e.useMemo = function(e, t) {
		return T.H.useMemo(e, t);
	}, e.useOptimistic = function(e, t) {
		return T.H.useOptimistic(e, t);
	}, e.useReducer = function(e, t, n) {
		return T.H.useReducer(e, t, n);
	}, e.useRef = function(e) {
		return T.H.useRef(e);
	}, e.useState = function(e) {
		return T.H.useState(e);
	}, e.useSyncExternalStore = function(e, t, n) {
		return T.H.useSyncExternalStore(e, t, n);
	}, e.useTransition = function() {
		return T.H.useTransition();
	}, e.version = "19.3.1";
})), u = /* @__PURE__ */ o(((e, t) => {
	t.exports = l();
})), d = /* @__PURE__ */ o(((e) => {
	function t(e, t) {
		var n = e.length;
		e.push(t);
		a: for (; 0 < n;) {
			var r = n - 1 >>> 1, a = e[r];
			if (0 < i(a, t)) e[r] = t, e[n] = a, n = r;
			else break a;
		}
	}
	function n(e) {
		return e.length === 0 ? null : e[0];
	}
	function r(e) {
		if (e.length === 0) return null;
		var t = e[0], n = e.pop();
		if (n !== t) {
			e[0] = n;
			a: for (var r = 0, a = e.length, o = a >>> 1; r < o;) {
				var s = 2 * (r + 1) - 1, c = e[s], l = s + 1, u = e[l];
				if (0 > i(c, n)) l < a && 0 > i(u, c) ? (e[r] = u, e[l] = n, r = l) : (e[r] = c, e[s] = n, r = s);
				else if (l < a && 0 > i(u, n)) e[r] = u, e[l] = n, r = l;
				else break a;
			}
		}
		return t;
	}
	function i(e, t) {
		var n = e.sortIndex - t.sortIndex;
		return n === 0 ? e.id - t.id : n;
	}
	if (e.unstable_now = void 0, typeof performance == "object" && typeof performance.now == "function") {
		var a = performance;
		e.unstable_now = function() {
			return a.now();
		};
	} else {
		var o = Date, s = o.now();
		e.unstable_now = function() {
			return o.now() - s;
		};
	}
	var c = [], l = [], u = 1, d = null, f = 3, p = !1, m = !1, h = !1, g = !1, _ = typeof setTimeout == "function" ? setTimeout : null, v = typeof clearTimeout == "function" ? clearTimeout : null, y = typeof setImmediate < "u" ? setImmediate : null;
	function b(e) {
		for (var i = n(l); i !== null;) {
			if (i.callback === null) r(l);
			else if (i.startTime <= e) r(l), i.sortIndex = i.expirationTime, t(c, i);
			else break;
			i = n(l);
		}
	}
	function x(e) {
		if (h = !1, b(e), !m) {
			if (n(c) !== null) m = !0, S || (S = !0, te());
			else {
				var t = n(l);
				t !== null && D(x, t.startTime - e);
			}
		}
	}
	var S = !1, C = -1, w = 5, T = -1;
	function ee() {
		return g ? !0 : !(e.unstable_now() - T < w);
	}
	function E() {
		if (g = !1, S) {
			var t = e.unstable_now();
			T = t;
			var i = !0;
			try {
				a: {
					m = !1, h && (h = !1, v(C), C = -1), p = !0;
					var a = f;
					try {
						b: {
							for (b(t), d = n(c); d !== null && !(d.expirationTime > t && ee());) {
								var o = d.callback;
								if (typeof o == "function") {
									d.callback = null, f = d.priorityLevel;
									var s = o(d.expirationTime <= t);
									if (t = e.unstable_now(), typeof s == "function") {
										d.callback = s, b(t), i = !0;
										break b;
									}
									d === n(c) && r(c), b(t);
								} else r(c);
								d = n(c);
							}
							if (d !== null) i = !0;
							else {
								var u = n(l);
								u !== null && D(x, u.startTime - t), i = !1;
							}
						}
						break a;
					} finally {
						d = null, f = a, p = !1;
					}
					i = void 0;
				}
			} finally {
				i ? te() : S = !1;
			}
		}
	}
	var te;
	if (typeof y == "function") te = function() {
		y(E);
	};
	else if (typeof MessageChannel < "u") {
		var ne = new MessageChannel(), re = ne.port2;
		ne.port1.onmessage = E, te = function() {
			re.postMessage(null);
		};
	} else te = function() {
		_(E, 0);
	};
	function D(t, n) {
		C = _(function() {
			t(e.unstable_now());
		}, n);
	}
	e.unstable_IdlePriority = 5, e.unstable_ImmediatePriority = 1, e.unstable_LowPriority = 4, e.unstable_NormalPriority = 3, e.unstable_Profiling = null, e.unstable_UserBlockingPriority = 2, e.unstable_cancelCallback = function(e) {
		e.callback = null;
	}, e.unstable_forceFrameRate = function(e) {
		0 > e || 125 < e ? console.error("forceFrameRate takes a positive int between 0 and 125, forcing frame rates higher than 125 fps is not supported") : w = 0 < e ? Math.floor(1e3 / e) : 5;
	}, e.unstable_getCurrentPriorityLevel = function() {
		return f;
	}, e.unstable_next = function(e) {
		switch (f) {
			case 1:
			case 2:
			case 3:
				var t = 3;
				break;
			default: t = f;
		}
		var n = f;
		f = t;
		try {
			return e();
		} finally {
			f = n;
		}
	}, e.unstable_requestPaint = function() {
		g = !0;
	}, e.unstable_runWithPriority = function(e, t) {
		switch (e) {
			case 1:
			case 2:
			case 3:
			case 4:
			case 5: break;
			default: e = 3;
		}
		var n = f;
		f = e;
		try {
			return t();
		} finally {
			f = n;
		}
	}, e.unstable_scheduleCallback = function(r, i, a) {
		var o = e.unstable_now();
		switch (typeof a == "object" && a ? (a = a.delay, a = typeof a == "number" && 0 < a ? o + a : o) : a = o, r) {
			case 1:
				var s = -1;
				break;
			case 2:
				s = 250;
				break;
			case 5:
				s = 1073741823;
				break;
			case 4:
				s = 1e4;
				break;
			default: s = 5e3;
		}
		return s = a + s, r = {
			id: u++,
			callback: i,
			priorityLevel: r,
			startTime: a,
			expirationTime: s,
			sortIndex: -1
		}, a > o ? (r.sortIndex = a, t(l, r), n(c) === null && r === n(l) && (h ? (v(C), C = -1) : h = !0, D(x, a - o))) : (r.sortIndex = s, t(c, r), m || p || (m = !0, S || (S = !0, te()))), r;
	}, e.unstable_shouldYield = ee, e.unstable_wrapCallback = function(e) {
		var t = f;
		return function() {
			var n = f;
			f = t;
			try {
				return e.apply(this, arguments);
			} finally {
				f = n;
			}
		};
	};
})), f = /* @__PURE__ */ o(((e, t) => {
	t.exports = d();
})), p = /* @__PURE__ */ o(((e) => {
	var t = u();
	function n(e) {
		var t = "https://react.dev/errors/" + e;
		if (1 < arguments.length) {
			t += "?args[]=" + encodeURIComponent(arguments[1]);
			for (var n = 2; n < arguments.length; n++) t += "&args[]=" + encodeURIComponent(arguments[n]);
		}
		return "Minified React error #" + e + "; visit " + t + " for the full message or use the non-minified dev environment for full errors and additional helpful warnings.";
	}
	function r() {}
	var i = {
		d: {
			f: r,
			r: function() {
				throw Error(n(522));
			},
			D: r,
			C: r,
			L: r,
			m: r,
			X: r,
			S: r,
			M: r
		},
		p: 0,
		findDOMNode: null
	}, a = Symbol.for("react.portal"), o = Symbol.for("react.recoverable"), s = Symbol.for("react.optimistic_key");
	function c(e, t, n) {
		var r = 3 < arguments.length && arguments[3] !== void 0 ? arguments[3] : null;
		return {
			$$typeof: a,
			key: r == null ? null : r === s ? s : "" + r,
			children: e,
			containerInfo: t,
			implementation: n
		};
	}
	var l = t.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
	function d(e, t) {
		if (e === "font") return "";
		if (typeof t == "string") return t === "use-credentials" ? t : "";
	}
	e.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE = i, e.browser = function(e) {
		return {
			$$typeof: o,
			_reason: e
		};
	}, e.createPortal = function(e, t) {
		var r = 2 < arguments.length && arguments[2] !== void 0 ? arguments[2] : null;
		if (!t || t.nodeType !== 1 && t.nodeType !== 9 && t.nodeType !== 11) throw Error(n(299));
		return c(e, t, null, r);
	}, e.flushSync = function(e) {
		var t = l.T, n = i.p;
		try {
			if (l.T = null, i.p = 2, e) return e();
		} finally {
			l.T = t, i.p = n, i.d.f();
		}
	}, e.preconnect = function(e, t) {
		typeof e == "string" && (t ? (t = t.crossOrigin, t = typeof t == "string" ? t === "use-credentials" ? t : "" : void 0) : t = null, i.d.C(e, t));
	}, e.prefetchDNS = function(e) {
		typeof e == "string" && i.d.D(e);
	}, e.preinit = function(e, t) {
		if (typeof e == "string" && t && typeof t.as == "string") {
			var n = t.as, r = d(n, t.crossOrigin), a = typeof t.integrity == "string" ? t.integrity : void 0, o = typeof t.fetchPriority == "string" ? t.fetchPriority : void 0;
			n === "style" ? i.d.S(e, typeof t.precedence == "string" ? t.precedence : void 0, {
				crossOrigin: r,
				integrity: a,
				fetchPriority: o
			}) : n === "script" && i.d.X(e, {
				crossOrigin: r,
				integrity: a,
				fetchPriority: o,
				nonce: typeof t.nonce == "string" ? t.nonce : void 0
			});
		}
	}, e.preinitModule = function(e, t) {
		if (typeof e == "string") {
			if (typeof t == "object" && t) {
				if (t.as == null || t.as === "script") {
					var n = d(t.as, t.crossOrigin);
					i.d.M(e, {
						crossOrigin: n,
						integrity: typeof t.integrity == "string" ? t.integrity : void 0,
						nonce: typeof t.nonce == "string" ? t.nonce : void 0,
						fetchPriority: typeof t.fetchPriority == "string" ? t.fetchPriority : void 0
					});
				}
			} else t ?? i.d.M(e);
		}
	}, e.preload = function(e, t) {
		if (typeof e == "string" && typeof t == "object" && t && typeof t.as == "string") {
			var n = t.as, r = d(n, t.crossOrigin);
			i.d.L(e, n, {
				crossOrigin: r,
				integrity: typeof t.integrity == "string" ? t.integrity : void 0,
				nonce: typeof t.nonce == "string" ? t.nonce : void 0,
				type: typeof t.type == "string" ? t.type : void 0,
				fetchPriority: typeof t.fetchPriority == "string" ? t.fetchPriority : void 0,
				referrerPolicy: typeof t.referrerPolicy == "string" ? t.referrerPolicy : void 0,
				imageSrcSet: typeof t.imageSrcSet == "string" ? t.imageSrcSet : void 0,
				imageSizes: typeof t.imageSizes == "string" ? t.imageSizes : void 0,
				media: typeof t.media == "string" ? t.media : void 0
			});
		}
	}, e.preloadModule = function(e, t) {
		if (typeof e == "string") {
			if (t) {
				var n = d(t.as, t.crossOrigin);
				i.d.m(e, {
					as: typeof t.as == "string" && t.as !== "script" ? t.as : void 0,
					crossOrigin: n,
					integrity: typeof t.integrity == "string" ? t.integrity : void 0,
					nonce: typeof t.nonce == "string" ? t.nonce : void 0,
					fetchPriority: typeof t.fetchPriority == "string" ? t.fetchPriority : void 0
				});
			} else i.d.m(e);
		}
	}, e.requestFormReset = function(e) {
		i.d.r(e);
	}, e.unstable_batchedUpdates = function(e, t) {
		return e(t);
	}, e.useFormState = function(e, t, n) {
		return l.H.useFormState(e, t, n);
	}, e.useFormStatus = function() {
		return l.H.useHostTransitionStatus();
	}, e.version = "19.3.1";
})), m = /* @__PURE__ */ o(((e, t) => {
	function n() {
		if (typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ < "u" && typeof __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE == "function") try {
			__REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE(n);
		} catch (e) {
			console.error(e);
		}
	}
	n(), t.exports = p();
})), h = /* @__PURE__ */ o(((e) => {
	var t = f(), n = u(), r = m();
	function i(e) {
		var t = "https://react.dev/errors/" + e;
		if (1 < arguments.length) {
			t += "?args[]=" + encodeURIComponent(arguments[1]);
			for (var n = 2; n < arguments.length; n++) t += "&args[]=" + encodeURIComponent(arguments[n]);
		}
		return "Minified React error #" + e + "; visit " + t + " for the full message or use the non-minified dev environment for full errors and additional helpful warnings.";
	}
	function a(e) {
		return !(!e || e.nodeType !== 1 && e.nodeType !== 9 && e.nodeType !== 11);
	}
	function o(e) {
		for (var t = e, n = t; n && !n.alternate;) t = n, t.flags & 4098 && (e = t.return), n = t.return;
		for (; t.return;) t = t.return;
		return t.tag === 3 ? e : null;
	}
	function s(e) {
		if (e.tag === 13) {
			var t = e.memoizedState;
			if (t === null && (e = e.alternate, e !== null && (t = e.memoizedState)), t !== null) return t.dehydrated;
		}
		return null;
	}
	function c(e) {
		if (e.tag === 31) {
			var t = e.memoizedState;
			if (t === null && (e = e.alternate, e !== null && (t = e.memoizedState)), t !== null) return t.dehydrated;
		}
		return null;
	}
	function l(e) {
		if (o(e) !== e) throw Error(i(188));
	}
	function d(e) {
		var t = e.alternate;
		if (!t) {
			if (t = o(e), t === null) throw Error(i(188));
			return t === e ? e : null;
		}
		for (var n = e, r = t;;) {
			var a = n.return;
			if (a === null) break;
			var s = a.alternate;
			if (s === null) {
				if (r = a.return, r !== null) {
					n = r;
					continue;
				}
				break;
			}
			if (a.child === s.child) {
				for (s = a.child; s;) {
					if (s === n) return l(a), e;
					if (s === r) return l(a), t;
					s = s.sibling;
				}
				throw Error(i(188));
			}
			if (n.return !== r.return) n = a, r = s;
			else {
				for (var c = !1, u = a.child; u;) {
					if (u === n) {
						c = !0, n = a, r = s;
						break;
					}
					if (u === r) {
						c = !0, r = a, n = s;
						break;
					}
					u = u.sibling;
				}
				if (!c) {
					for (u = s.child; u;) {
						if (u === n) {
							c = !0, n = s, r = a;
							break;
						}
						if (u === r) {
							c = !0, r = s, n = a;
							break;
						}
						u = u.sibling;
					}
					if (!c) throw Error(i(189));
				}
			}
			if (n.alternate !== r) throw Error(i(190));
		}
		if (n.tag !== 3) throw Error(i(188));
		return n.stateNode.current === n ? e : t;
	}
	function p(e) {
		var t = e.tag;
		if (t === 5 || t === 26 || t === 27 || t === 6) return e;
		for (e = e.child; e !== null;) {
			if (t = p(e), t !== null) return t;
			e = e.sibling;
		}
		return null;
	}
	function h(e, t, n, r, i, a) {
		for (; e !== null;) {
			if ((e.tag === 5 || e.tag === 27 || e.tag === 6) && n(e, r, i, a) || (e.tag !== 22 || e.memoizedState === null) && (t || e.tag !== 5 && e.tag !== 27) && h(e.child, t, n, r, i, a)) return !0;
			e = e.sibling;
		}
		return !1;
	}
	function g(e) {
		for (e = e.return; e !== null;) {
			if (e.tag === 3 || e.tag === 5 || e.tag === 27) return e;
			e = e.return;
		}
		return null;
	}
	function _(e) {
		var t = !1;
		for (e = e.return; e !== null && (e.tag === 4 && (t = !0), e.tag !== 3 && e.tag !== 5 && e.tag !== 27);) e = e.return;
		return t;
	}
	function v(e) {
		var t = [null, null], n = g(e);
		return n === null || y(t, e, n.child, { foundSelf: !1 }), t;
	}
	function y(e, t, n, r) {
		for (; n !== null;) {
			if (n === t) r.foundSelf = !0;
			else if (n.tag === 5 || n.tag === 27 || n.tag === 6) {
				if (r.foundSelf) return e[1] = n, !0;
				e[0] = n;
			} else if ((n.tag !== 22 || n.memoizedState === null) && y(e, t, n.child, r)) return !0;
			n = n.sibling;
		}
		return !1;
	}
	function b(e) {
		switch (e.tag) {
			case 5:
			case 27:
			case 6: return e.stateNode;
			case 3: return e.stateNode.containerInfo;
			default: throw Error(i(559));
		}
	}
	var x = null, S = null;
	function C(e, t, n) {
		return e === n || e === t && (x = e, !0);
	}
	function w(e, t, n) {
		return e === n ? (S = e, !1) : e === t && (S !== null && (x = e), !0);
	}
	function T(e) {
		if (e === null) return null;
		do
			e = e === null ? null : e.return;
		while (e && e.tag !== 5 && e.tag !== 27 && e.tag !== 3);
		return e || null;
	}
	function ee(e, t, n) {
		for (var r = 0, i = e; i; i = n(i)) r++;
		i = 0;
		for (var a = t; a; a = n(a)) i++;
		for (; 0 < r - i;) e = n(e), r--;
		for (; 0 < i - r;) t = n(t), i--;
		for (; r--;) {
			if (e === t || t !== null && e === t.alternate) return e;
			e = n(e), t = n(t);
		}
		return null;
	}
	var E = Object.assign, te = Symbol.for("react.element"), ne = Symbol.for("react.transitional.element"), re = Symbol.for("react.portal"), D = Symbol.for("react.fragment"), ie = Symbol.for("react.strict_mode"), ae = Symbol.for("react.profiler"), oe = Symbol.for("react.consumer"), se = Symbol.for("react.context"), ce = Symbol.for("react.forward_ref"), le = Symbol.for("react.suspense"), ue = Symbol.for("react.suspense_list"), de = Symbol.for("react.memo"), fe = Symbol.for("react.lazy"), pe = Symbol.for("react.activity"), me = Symbol.for("react.legacy_hidden"), he = Symbol.for("react.memo_cache_sentinel"), O = Symbol.for("react.view_transition"), ge = Symbol.for("react.recoverable"), _e = Symbol.iterator;
	function ve(e) {
		return typeof e != "object" || !e ? null : (e = _e && e[_e] || e["@@iterator"], typeof e == "function" ? e : null);
	}
	var ye = Symbol.for("react.client.reference");
	function be(e) {
		if (e == null) return null;
		if (typeof e == "function") return e.$$typeof === ye ? null : e.displayName || e.name || null;
		if (typeof e == "string") return e;
		switch (e) {
			case D: return "Fragment";
			case ae: return "Profiler";
			case ie: return "StrictMode";
			case le: return "Suspense";
			case ue: return "SuspenseList";
			case pe: return "Activity";
			case O: return "ViewTransition";
		}
		if (typeof e == "object") switch (e.$$typeof) {
			case re: return "Portal";
			case se: return e.displayName || "Context";
			case oe: return (e._context.displayName || "Context") + ".Consumer";
			case ce:
				var t = e.render;
				return e = e.displayName, e ||= (e = t.displayName || t.name || "", e === "" ? "ForwardRef" : "ForwardRef(" + e + ")"), e;
			case de: return t = e.displayName || null, t === null ? be(e.type) || "Memo" : t;
			case fe:
				t = e._payload, e = e._init;
				try {
					return be(e(t));
				} catch {}
		}
		return null;
	}
	var xe = Array.isArray, k = n.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE, A = r.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE, Se = {
		pending: !1,
		data: null,
		method: null,
		action: null
	}, Ce = [], we = -1;
	function Te(e) {
		return { current: e };
	}
	function Ee(e) {
		0 > we || (e.current = Ce[we], Ce[we] = null, we--);
	}
	function j(e, t) {
		we++, Ce[we] = e.current, e.current = t;
	}
	var De = Te(null), Oe = Te(null), ke = Te(null), Ae = Te(null);
	function je(e, t) {
		switch (j(ke, t), j(Oe, e), j(De, null), t.nodeType) {
			case 9:
			case 11:
				e = (e = t.documentElement) && (e = e.namespaceURI) ? up(e) : 0;
				break;
			default: if (e = t.tagName, t = t.namespaceURI) t = up(t), e = dp(t, e);
			else switch (e) {
				case "svg":
					e = 1;
					break;
				case "math":
					e = 2;
					break;
				default: e = 0;
			}
		}
		Ee(De), j(De, e);
	}
	function Me() {
		Ee(De), Ee(Oe), Ee(ke);
	}
	function Ne(e) {
		var t = e.memoizedState;
		t !== null && (sh._currentValue = t.memoizedState, j(Ae, e)), t = De.current;
		var n = dp(t, e.type);
		t !== n && (j(Oe, e), j(De, n));
	}
	function Pe(e) {
		Oe.current === e && (Ee(De), Ee(Oe)), Ae.current === e && (Ee(Ae), sh._currentValue = Se);
	}
	var Fe, Ie;
	function Le(e) {
		if (Fe === void 0) try {
			throw Error();
		} catch (e) {
			var t = e.stack.trim().match(/\n( *(at )?)/);
			Fe = t && t[1] || "", Ie = -1 < e.stack.indexOf("\n    at") ? " (<anonymous>)" : -1 < e.stack.indexOf("@") ? "@unknown:0:0" : "";
		}
		return "\n" + Fe + e + Ie;
	}
	var Re = !1;
	function ze(e, t) {
		if (!e || Re) return "";
		Re = !0;
		var n = Error.prepareStackTrace;
		Error.prepareStackTrace = void 0;
		try {
			var r = { DetermineComponentFrameRoot: function() {
				try {
					if (t) {
						var n = function() {
							throw Error();
						};
						if (Object.defineProperty(n.prototype, "props", { set: function() {
							throw Error();
						} }), typeof Reflect == "object" && Reflect.construct) {
							try {
								Reflect.construct(n, []);
							} catch (e) {
								var r = e;
							}
							Reflect.construct(e, [], n);
						} else {
							try {
								n.call();
							} catch (e) {
								r = e;
							}
							n = !1;
							try {
								var i = Object.getOwnPropertyDescriptor(e.prototype, "props");
								Object.defineProperty(e.prototype, "props", {
									configurable: !0,
									set: function() {
										throw Error();
									}
								}), n = !0, new e();
							} finally {
								n && (i === void 0 ? delete e.prototype.props : Object.defineProperty(e.prototype, "props", i));
							}
						}
					} else {
						try {
							throw Error();
						} catch (e) {
							r = e;
						}
						(n = e()) && typeof n.catch == "function" && n.catch(function() {});
					}
				} catch (e) {
					if (e && r && typeof e.stack == "string") return [e.stack, r.stack];
				}
				return [null, null];
			} };
			r.DetermineComponentFrameRoot.displayName = "DetermineComponentFrameRoot";
			var i = Object.getOwnPropertyDescriptor(r.DetermineComponentFrameRoot, "name");
			i && i.configurable && Object.defineProperty(r.DetermineComponentFrameRoot, "name", { value: "DetermineComponentFrameRoot" });
			var a = r.DetermineComponentFrameRoot(), o = a[0], s = a[1];
			if (o && s) {
				var c = o.split("\n"), l = s.split("\n");
				for (i = r = 0; r < c.length && !c[r].includes("DetermineComponentFrameRoot");) r++;
				for (; i < l.length && !l[i].includes("DetermineComponentFrameRoot");) i++;
				if (r === c.length || i === l.length) for (r = c.length - 1, i = l.length - 1; 1 <= r && 0 <= i && c[r] !== l[i];) i--;
				for (; 1 <= r && 0 <= i; r--, i--) if (c[r] !== l[i]) {
					if (r !== 1 || i !== 1) do
						if (r--, i--, 0 > i || c[r] !== l[i]) {
							var u = "\n" + c[r].replace(" at new ", " at ");
							return e.displayName && u.includes("<anonymous>") && (u = u.replace("<anonymous>", e.displayName)), u;
						}
					while (1 <= r && 0 <= i);
					break;
				}
			}
		} finally {
			Re = !1, Error.prepareStackTrace = n;
		}
		return (n = e ? e.displayName || e.name : "") ? Le(n) : "";
	}
	function Be(e, t) {
		switch (e.tag) {
			case 26:
			case 27:
			case 5: return Le(e.type);
			case 16: return Le("Lazy");
			case 13: return e.child !== t && t !== null ? Le("Suspense Fallback") : Le("Suspense");
			case 19: return Le("SuspenseList");
			case 0:
			case 15: return ze(e.type, !1);
			case 11: return ze(e.type.render, !1);
			case 1: return ze(e.type, !0);
			case 31: return Le("Activity");
			case 30: return Le("ViewTransition");
			default: return "";
		}
	}
	function Ve(e) {
		try {
			var t = "", n = null;
			do
				t += Be(e, n), n = e, e = e.return;
			while (e);
			return t;
		} catch (e) {
			return "\nError generating stack: " + e.message + "\n" + e.stack;
		}
	}
	var He = Object.prototype.hasOwnProperty, Ue = t.unstable_scheduleCallback, We = t.unstable_cancelCallback, Ge = t.unstable_shouldYield, Ke = t.unstable_requestPaint, qe = t.unstable_now, Je = t.unstable_getCurrentPriorityLevel, Ye = t.unstable_ImmediatePriority, Xe = t.unstable_UserBlockingPriority, Ze = t.unstable_NormalPriority, Qe = t.unstable_LowPriority, $e = t.unstable_IdlePriority, et = t.log, tt = t.unstable_setDisableYieldValue, nt = null, rt = null;
	function it(e) {
		if (typeof et == "function" && tt(e), rt && typeof rt.setStrictMode == "function") try {
			rt.setStrictMode(nt, e);
		} catch {}
	}
	var at = Math.clz32 ? Math.clz32 : ct, ot = Math.log, st = Math.LN2;
	function ct(e) {
		return e >>>= 0, e === 0 ? 32 : 31 - (ot(e) / st | 0) | 0;
	}
	var lt = 256, ut = 262144, dt = 4194304;
	function ft(e) {
		var t = e & 42;
		if (t !== 0) return t;
		switch (e & -e) {
			case 1: return 1;
			case 2: return 2;
			case 4: return 4;
			case 8: return 8;
			case 16: return 16;
			case 32: return 32;
			case 64: return 64;
			case 128: return 128;
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072: return e & -e;
			case 262144:
			case 524288:
			case 1048576:
			case 2097152: return e & 3932160;
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432: return e & 62914560;
			case 67108864: return 67108864;
			case 134217728: return 134217728;
			case 268435456: return 268435456;
			case 536870912: return 536870912;
			case 1073741824: return 0;
			default: return e;
		}
	}
	function pt(e, t, n) {
		var r = e.pendingLanes;
		if (r === 0) return 0;
		var i = 0, a = e.suspendedLanes, o = e.pingedLanes;
		e = e.warmLanes;
		var s = r & 134217727;
		return s === 0 ? (s = r & ~a, s === 0 ? o === 0 ? n || (n = r & ~e, n !== 0 && (i = ft(n))) : i = ft(o) : i = ft(s)) : (r = s & ~a, r === 0 ? (o &= s, o === 0 ? n || (n = s & ~e, n !== 0 && (i = ft(n))) : i = ft(o)) : i = ft(r)), i === 0 ? 0 : t !== 0 && t !== i && (t & a) === 0 && (a = i & -i, n = t & -t, a >= n || a === 32 && n & 4194048) ? t : i;
	}
	function mt(e, t) {
		return (e.pendingLanes & ~(e.suspendedLanes & ~e.pingedLanes) & t) === 0;
	}
	function ht(e, t) {
		t & 8 && (t |= t & 32);
		var n = e.entangledLanes;
		if (n !== 0) for (e = e.entanglements, n &= t; 0 < n;) {
			var r = 31 - at(n), i = 1 << r;
			t |= e[r], n &= ~i;
		}
		return t;
	}
	function gt(e, t) {
		switch (e) {
			case 1:
			case 2:
			case 4:
			case 8:
			case 64: return t + 250;
			case 16:
			case 32:
			case 128:
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072:
			case 262144:
			case 524288:
			case 1048576:
			case 2097152: return t + 5e3;
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432: return -1;
			case 67108864:
			case 134217728:
			case 268435456:
			case 536870912:
			case 1073741824: return -1;
			default: return -1;
		}
	}
	function _t() {
		var e = dt;
		return dt <<= 1, !(dt & 62914560) && (dt = 4194304), e;
	}
	function vt(e) {
		for (var t = [], n = 0; 31 > n; n++) t.push(e);
		return t;
	}
	function M(e, t) {
		e.pendingLanes |= t, t !== 268435456 && (e.suspendedLanes = 0, e.pingedLanes = 0, e.warmLanes = 0);
	}
	function yt(e, t, n, r, i, a) {
		var o = e.pendingLanes;
		e.pendingLanes = n, e.suspendedLanes = 0, e.pingedLanes = 0, e.warmLanes = 0, e.expiredLanes &= n, e.entangledLanes &= n, e.errorRecoveryDisabledLanes &= n, e.shellSuspendCounter = 0;
		var s = e.entanglements, c = e.expirationTimes, l = e.hiddenUpdates;
		for (n = o & ~n; 0 < n;) {
			var u = 31 - at(n), d = 1 << u;
			s[u] = 0, c[u] = -1;
			var f = l[u];
			if (f !== null) for (l[u] = null, u = 0; u < f.length; u++) {
				var p = f[u];
				p !== null && (p.lane &= -536870913);
			}
			n &= ~d;
		}
		r !== 0 && bt(e, r, 0), a !== 0 && i === 0 && e.tag !== 0 && (e.suspendedLanes |= a & ~(o & ~t));
	}
	function bt(e, t, n) {
		e.pendingLanes |= t, e.suspendedLanes &= ~t;
		var r = 31 - at(t);
		e.entangledLanes |= t, e.entanglements[r] = e.entanglements[r] | 1073741824 | n & 261930;
	}
	function xt(e, t) {
		var n = e.entangledLanes |= t;
		for (e = e.entanglements; n;) {
			var r = 31 - at(n), i = 1 << r;
			i & t | e[r] & t && (e[r] |= t), n &= ~i;
		}
	}
	function St(e, t) {
		var n = t & -t;
		return n = n & 42 ? 1 : Ct(n), (n & (e.suspendedLanes | t)) === 0 ? n : 0;
	}
	function Ct(e) {
		switch (e) {
			case 2:
				e = 1;
				break;
			case 8:
				e = 4;
				break;
			case 32:
				e = 16;
				break;
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072:
			case 262144:
			case 524288:
			case 1048576:
			case 2097152:
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432:
				e = 128;
				break;
			case 268435456:
				e = 134217728;
				break;
			default: e = 0;
		}
		return e;
	}
	function wt(e) {
		return e &= -e, 2 < e ? 8 < e ? e & 134217727 ? 32 : 268435456 : 8 : 2;
	}
	function Tt() {
		var e = A.p;
		return e === 0 ? (e = window.event, e === void 0 ? 32 : Ch(e.type)) : e;
	}
	function Et(e, t) {
		var n = A.p;
		try {
			return A.p = e, t();
		} finally {
			A.p = n;
		}
	}
	var Dt = Math.random().toString(36).slice(2), Ot = "__reactFiber$" + Dt, kt = "__reactProps$" + Dt, At = "__reactContainer$" + Dt, jt = "__reactEvents$" + Dt, Mt = "__reactListeners$" + Dt, Nt = "__reactHandles$" + Dt, Pt = "__reactResources$" + Dt, Ft = "__reactMarker$" + Dt, It = "__reactLoad$" + Dt;
	function Lt(e) {
		delete e[Ot], delete e[kt], delete e[Mt], delete e[Nt];
	}
	function Rt(e) {
		var t;
		if (t = e[Ot]) return t;
		for (var n = e.parentNode; n;) {
			if (t = n[At] || n[Ot]) {
				if (n = t.alternate, t.child !== null || n !== null && n.child !== null) for (e = fm(e); e !== null;) {
					if (n = e[Ot]) return n;
					e = fm(e);
				}
				return t;
			}
			e = n, n = e.parentNode;
		}
		return null;
	}
	function zt(e) {
		if (e = e[Ot] || e[At]) {
			var t = e.tag;
			if (t === 5 || t === 6 || t === 13 || t === 31 || t === 26 || t === 27 || t === 3) return e;
		}
		return null;
	}
	function Bt(e) {
		var t = e.tag;
		if (t === 5 || t === 26 || t === 27 || t === 6) return e.stateNode;
		throw Error(i(33));
	}
	function Vt(e) {
		var t = e[Pt];
		return t ||= e[Pt] = {
			hoistableStyles: /* @__PURE__ */ new Map(),
			hoistableScripts: /* @__PURE__ */ new Map()
		}, t;
	}
	function Ht(e) {
		e[Ft] = !0;
	}
	function Ut(e) {
		e[It] = void 0;
	}
	var Wt = /* @__PURE__ */ new Set(), Gt = {};
	function Kt(e, t) {
		qt(e, t), qt(e + "Capture", t);
	}
	function qt(e, t) {
		for (Gt[e] = t, e = 0; e < t.length; e++) Wt.add(t[e]);
	}
	var Jt = RegExp("^[:A-Z_a-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u02FF\\u0370-\\u037D\\u037F-\\u1FFF\\u200C-\\u200D\\u2070-\\u218F\\u2C00-\\u2FEF\\u3001-\\uD7FF\\uF900-\\uFDCF\\uFDF0-\\uFFFD][:A-Z_a-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u02FF\\u0370-\\u037D\\u037F-\\u1FFF\\u200C-\\u200D\\u2070-\\u218F\\u2C00-\\u2FEF\\u3001-\\uD7FF\\uF900-\\uFDCF\\uFDF0-\\uFFFD\\-.0-9\\u00B7\\u0300-\\u036F\\u203F-\\u2040]*$"), Yt = {}, Xt = {};
	function Zt(e) {
		return He.call(Xt, e) ? !0 : He.call(Yt, e) ? !1 : Jt.test(e) ? Xt[e] = !0 : (Yt[e] = !0, !1);
	}
	var N = !1;
	function Qt() {
		var e = N;
		return N = !1, e;
	}
	function $t(e, t, n) {
		if (Zt(t)) {
			if (n === null) e.removeAttribute(t);
			else {
				switch (typeof n) {
					case "undefined":
					case "function":
					case "symbol":
						e.removeAttribute(t);
						return;
					case "boolean":
						var r = t.toLowerCase().slice(0, 5);
						if (r !== "data-" && r !== "aria-") {
							e.removeAttribute(t);
							return;
						}
				}
				e.setAttribute(t, n);
			}
		}
	}
	function en(e, t, n) {
		if (n === null) e.removeAttribute(t);
		else {
			switch (typeof n) {
				case "undefined":
				case "function":
				case "symbol":
				case "boolean":
					e.removeAttribute(t);
					return;
			}
			e.setAttribute(t, n);
		}
	}
	function tn(e, t, n, r) {
		if (r === null) e.removeAttribute(n);
		else {
			switch (typeof r) {
				case "undefined":
				case "function":
				case "symbol":
				case "boolean":
					e.removeAttribute(n);
					return;
			}
			e.setAttributeNS(t, n, r);
		}
	}
	function nn(e) {
		switch (typeof e) {
			case "bigint":
			case "boolean":
			case "number":
			case "string":
			case "undefined": return e;
			case "object": return e;
			default: return "";
		}
	}
	function rn(e) {
		var t = e.type;
		return (e = e.nodeName) && e.toLowerCase() === "input" && (t === "checkbox" || t === "radio");
	}
	function an(e, t, n) {
		var r = Object.getOwnPropertyDescriptor(e.constructor.prototype, t);
		if (!e.hasOwnProperty(t) && r !== void 0 && typeof r.get == "function" && typeof r.set == "function") {
			var i = r.get, a = r.set;
			return Object.defineProperty(e, t, {
				configurable: !0,
				get: function() {
					return i.call(this);
				},
				set: function(e) {
					n = "" + e, a.call(this, e);
				}
			}), Object.defineProperty(e, t, { enumerable: r.enumerable }), {
				getValue: function() {
					return n;
				},
				setValue: function(e) {
					n = "" + e;
				},
				stopTracking: function() {
					e._valueTracker = null, delete e[t];
				}
			};
		}
	}
	function on(e) {
		if (!e._valueTracker) {
			var t = rn(e) ? "checked" : "value";
			e._valueTracker = an(e, t, "" + e[t]);
		}
	}
	function sn(e) {
		if (!e) return !1;
		var t = e._valueTracker;
		if (!t) return !0;
		var n = t.getValue(), r = "";
		return e && (r = rn(e) ? e.checked ? "true" : "false" : e.value), e = r, e !== n && (t.setValue(e), !0);
	}
	var cn = /[\n"\\]/g;
	function ln(e) {
		return e.replace(cn, function(e) {
			return "\\" + e.charCodeAt(0).toString(16) + " ";
		});
	}
	function un(e, t, n, r, i, a, o, s) {
		e.name = "", o != null && typeof o != "function" && typeof o != "symbol" && typeof o != "boolean" ? e.type = o : e.removeAttribute("type"), t == null ? o !== "submit" && o !== "reset" || e.removeAttribute("value") : o === "number" ? (t === 0 && e.value === "" || e.value != t) && (e.value = "" + nn(t)) : e.value !== "" + nn(t) && (e.value = "" + nn(t)), t == null ? n == null ? r != null && e.removeAttribute("value") : fn(e, nn(n)) : o === "number" && e.value == t ? fn(e, nn(e.value)) : fn(e, nn(t)), i == null && a != null && (e.defaultChecked = !!a), i != null && (e.checked = i && typeof i != "function" && typeof i != "symbol"), s != null && typeof s != "function" && typeof s != "symbol" && typeof s != "boolean" ? e.name = "" + nn(s) : e.removeAttribute("name");
	}
	function dn(e, t, n, r, i, a, o, s) {
		if (a != null && typeof a != "function" && typeof a != "symbol" && typeof a != "boolean" && (e.type = a), t != null || n != null) {
			if (!(a !== "submit" && a !== "reset" || t != null)) {
				on(e);
				return;
			}
			n = n == null ? "" : "" + nn(n), t = t == null ? n : "" + nn(t), s || t === e.value || (e.value = t), e.defaultValue = t;
		}
		r ??= i, r = typeof r != "function" && typeof r != "symbol" && !!r, e.checked = s ? e.checked : !!r, e.defaultChecked = !!r, o != null && typeof o != "function" && typeof o != "symbol" && typeof o != "boolean" && (e.name = o), on(e);
	}
	function fn(e, t) {
		e.defaultValue !== "" + t && (e.defaultValue = "" + t);
	}
	function pn(e, t, n, r) {
		if (e = e.options, t) {
			t = {};
			for (var i = 0; i < n.length; i++) t["$" + n[i]] = !0;
			for (n = 0; n < e.length; n++) i = t.hasOwnProperty("$" + e[n].value), e[n].selected !== i && (e[n].selected = i), i && r && (e[n].defaultSelected = !0);
		} else {
			for (n = "" + nn(n), t = null, i = 0; i < e.length; i++) {
				if (e[i].value === n) {
					e[i].selected = !0, r && (e[i].defaultSelected = !0);
					return;
				}
				t !== null || e[i].disabled || (t = e[i]);
			}
			t !== null && (t.selected = !0);
		}
	}
	function mn(e, t, n) {
		t != null && (t = "" + nn(t), t !== e.value && (e.value = t), n == null) ? e.defaultValue !== t && (e.defaultValue = t) : e.defaultValue = n == null ? "" : "" + nn(n);
	}
	function hn(e, t, n, r) {
		if (t == null) {
			if (r != null) {
				if (n != null) throw Error(i(92));
				if (xe(r)) {
					if (1 < r.length) throw Error(i(93));
					r = r[0];
				}
				n = r;
			}
			n ??= "", t = n;
		}
		n = nn(t), e.defaultValue = n, r = e.textContent, r === n && r !== "" && r !== null && (e.value = r), on(e);
	}
	function gn(e, t) {
		if (t) {
			var n = e.firstChild;
			if (n && n === e.lastChild && n.nodeType === 3) {
				n.nodeValue = t;
				return;
			}
		}
		e.textContent = t;
	}
	var _n = new Set("animationIterationCount aspectRatio borderImageOutset borderImageSlice borderImageWidth boxFlex boxFlexGroup boxOrdinalGroup columnCount columns flex flexGrow flexPositive flexShrink flexNegative flexOrder gridArea gridRow gridRowEnd gridRowSpan gridRowStart gridColumn gridColumnEnd gridColumnSpan gridColumnStart fontWeight lineClamp lineHeight opacity order orphans scale tabSize widows zIndex zoom fillOpacity floodOpacity stopOpacity strokeDasharray strokeDashoffset strokeMiterlimit strokeOpacity strokeWidth MozAnimationIterationCount MozBoxFlex MozBoxFlexGroup MozLineClamp msAnimationIterationCount msFlex msZoom msFlexGrow msFlexNegative msFlexOrder msFlexPositive msFlexShrink msGridColumn msGridColumnSpan msGridRow msGridRowSpan WebkitAnimationIterationCount WebkitBoxFlex WebKitBoxFlexGroup WebkitBoxOrdinalGroup WebkitColumnCount WebkitColumns WebkitFlex WebkitFlexGrow WebkitFlexPositive WebkitFlexShrink WebkitLineClamp".split(" "));
	function vn(e, t, n) {
		var r = t.indexOf("--") === 0;
		n == null || typeof n == "boolean" || n === "" ? r ? e.setProperty(t, "") : t === "float" ? e.cssFloat = "" : e[t] = "" : r ? e.setProperty(t, n) : typeof n != "number" || n === 0 || _n.has(t) ? t === "float" ? e.cssFloat = n : e[t] = ("" + n).trim() : e[t] = n + "px";
	}
	function yn(e, t, n) {
		if (t != null && typeof t != "object") throw Error(i(62));
		if (e = e.style, n != null) {
			for (var r in n) !n.hasOwnProperty(r) || t != null && t.hasOwnProperty(r) || (r.indexOf("--") === 0 ? e.setProperty(r, "") : r === "float" ? e.cssFloat = "" : e[r] = "", N = !0);
			for (var a in t) r = t[a], t.hasOwnProperty(a) && n[a] !== r && (vn(e, a, r), N = !0);
		} else for (var o in t) t.hasOwnProperty(o) && vn(e, o, t[o]);
	}
	function bn(e) {
		if (e.indexOf("-") === -1) return !1;
		switch (e) {
			case "annotation-xml":
			case "color-profile":
			case "font-face":
			case "font-face-src":
			case "font-face-uri":
			case "font-face-format":
			case "font-face-name":
			case "missing-glyph": return !1;
			default: return !0;
		}
	}
	var xn = /* @__PURE__ */ new Map([
		["acceptCharset", "accept-charset"],
		["htmlFor", "for"],
		["httpEquiv", "http-equiv"],
		["crossOrigin", "crossorigin"],
		["accentHeight", "accent-height"],
		["alignmentBaseline", "alignment-baseline"],
		["arabicForm", "arabic-form"],
		["baselineShift", "baseline-shift"],
		["capHeight", "cap-height"],
		["clipPath", "clip-path"],
		["clipRule", "clip-rule"],
		["colorInterpolation", "color-interpolation"],
		["colorInterpolationFilters", "color-interpolation-filters"],
		["colorProfile", "color-profile"],
		["colorRendering", "color-rendering"],
		["dominantBaseline", "dominant-baseline"],
		["enableBackground", "enable-background"],
		["fillOpacity", "fill-opacity"],
		["fillRule", "fill-rule"],
		["floodColor", "flood-color"],
		["floodOpacity", "flood-opacity"],
		["fontFamily", "font-family"],
		["fontSize", "font-size"],
		["fontSizeAdjust", "font-size-adjust"],
		["fontStretch", "font-stretch"],
		["fontStyle", "font-style"],
		["fontVariant", "font-variant"],
		["fontWeight", "font-weight"],
		["glyphName", "glyph-name"],
		["glyphOrientationHorizontal", "glyph-orientation-horizontal"],
		["glyphOrientationVertical", "glyph-orientation-vertical"],
		["horizAdvX", "horiz-adv-x"],
		["horizOriginX", "horiz-origin-x"],
		["imageRendering", "image-rendering"],
		["letterSpacing", "letter-spacing"],
		["lightingColor", "lighting-color"],
		["markerEnd", "marker-end"],
		["markerMid", "marker-mid"],
		["markerStart", "marker-start"],
		["maskType", "mask-type"],
		["overlinePosition", "overline-position"],
		["overlineThickness", "overline-thickness"],
		["paintOrder", "paint-order"],
		["panose-1", "panose-1"],
		["pointerEvents", "pointer-events"],
		["renderingIntent", "rendering-intent"],
		["shapeRendering", "shape-rendering"],
		["stopColor", "stop-color"],
		["stopOpacity", "stop-opacity"],
		["strikethroughPosition", "strikethrough-position"],
		["strikethroughThickness", "strikethrough-thickness"],
		["strokeDasharray", "stroke-dasharray"],
		["strokeDashoffset", "stroke-dashoffset"],
		["strokeLinecap", "stroke-linecap"],
		["strokeLinejoin", "stroke-linejoin"],
		["strokeMiterlimit", "stroke-miterlimit"],
		["strokeOpacity", "stroke-opacity"],
		["strokeWidth", "stroke-width"],
		["textAnchor", "text-anchor"],
		["textDecoration", "text-decoration"],
		["textRendering", "text-rendering"],
		["transformOrigin", "transform-origin"],
		["underlinePosition", "underline-position"],
		["underlineThickness", "underline-thickness"],
		["unicodeBidi", "unicode-bidi"],
		["unicodeRange", "unicode-range"],
		["unitsPerEm", "units-per-em"],
		["vAlphabetic", "v-alphabetic"],
		["vHanging", "v-hanging"],
		["vIdeographic", "v-ideographic"],
		["vMathematical", "v-mathematical"],
		["vectorEffect", "vector-effect"],
		["vertAdvY", "vert-adv-y"],
		["vertOriginX", "vert-origin-x"],
		["vertOriginY", "vert-origin-y"],
		["wordSpacing", "word-spacing"],
		["writingMode", "writing-mode"],
		["xmlnsXlink", "xmlns:xlink"],
		["xHeight", "x-height"]
	]), Sn = /^[\u0000-\u001F ]*j[\r\n\t]*a[\r\n\t]*v[\r\n\t]*a[\r\n\t]*s[\r\n\t]*c[\r\n\t]*r[\r\n\t]*i[\r\n\t]*p[\r\n\t]*t[\r\n\t]*:/i;
	function Cn(e) {
		return Sn.test("" + e) ? "javascript:throw new Error('React has blocked a javascript: URL as a security precaution.')" : e;
	}
	function wn() {}
	var Tn = null;
	function En(e) {
		return e = e.target || e.srcElement || window, e.correspondingUseElement && (e = e.correspondingUseElement), e.nodeType === 3 ? e.parentNode : e;
	}
	var Dn = null, On = null;
	function kn(e) {
		var t = zt(e);
		if (t && (e = t.stateNode)) {
			var n = e[kt] || null;
			a: switch (e = t.stateNode, t.type) {
				case "input":
					if (un(e, n.value, n.defaultValue, n.defaultValue, n.checked, n.defaultChecked, n.type, n.name), t = n.name, n.type === "radio" && t != null) {
						for (n = e; n.parentNode;) n = n.parentNode;
						for (n = n.querySelectorAll("input[name=\"" + ln("" + t) + "\"][type=\"radio\"]"), t = 0; t < n.length; t++) {
							var r = n[t];
							if (r !== e && r.form === e.form) {
								var a = r[kt] || null;
								if (!a) throw Error(i(90));
								un(r, a.value, a.defaultValue, a.defaultValue, a.checked, a.defaultChecked, a.type, a.name);
							}
						}
						for (t = 0; t < n.length; t++) r = n[t], r.form === e.form && sn(r);
					}
					break a;
				case "textarea":
					mn(e, n.value, n.defaultValue);
					break a;
				case "select": t = n.value, t != null && pn(e, !!n.multiple, t, !1);
			}
		}
	}
	var An = !1;
	function jn(e, t, n) {
		if (An) return e(t, n);
		An = !0;
		try {
			return e(t);
		} finally {
			if (An = !1, (Dn !== null || On !== null) && (zd(), Dn && (t = Dn, e = On, On = Dn = null, kn(t), e))) for (t = 0; t < e.length; t++) kn(e[t]);
		}
	}
	function Mn(e, t) {
		var n = e.stateNode;
		if (n === null) return null;
		var r = n[kt] || null;
		if (r === null) return null;
		n = r[t];
		a: switch (t) {
			case "onClick":
			case "onClickCapture":
			case "onDoubleClick":
			case "onDoubleClickCapture":
			case "onMouseDown":
			case "onMouseDownCapture":
			case "onMouseMove":
			case "onMouseMoveCapture":
			case "onMouseUp":
			case "onMouseUpCapture":
			case "onMouseEnter":
				(r = !r.disabled) || (e = e.type, r = e !== "button" && e !== "input" && e !== "select" && e !== "textarea"), e = !r;
				break a;
			default: e = !1;
		}
		if (e) return null;
		if (n && typeof n != "function") throw Error(i(231, t, typeof n));
		return n;
	}
	var Nn = typeof window < "u" && window.document !== void 0 && window.document.createElement !== void 0, Pn = !1;
	if (Nn) try {
		var Fn = {};
		Object.defineProperty(Fn, "passive", { get: function() {
			Pn = !0;
		} }), window.addEventListener("test", Fn, Fn), window.removeEventListener("test", Fn, Fn);
	} catch {
		Pn = !1;
	}
	var In = null, Ln = null, Rn = null;
	function zn() {
		if (Rn) return Rn;
		var e, t = Ln, n = t.length, r, i = "value" in In ? In.value : In.textContent, a = i.length;
		for (e = 0; e < n && t[e] === i[e]; e++);
		var o = n - e;
		for (r = 1; r <= o && t[n - r] === i[a - r]; r++);
		return Rn = i.slice(e, 1 < r ? 1 - r : void 0);
	}
	function Bn(e) {
		var t = e.keyCode;
		return "charCode" in e ? (e = e.charCode, e === 0 && t === 13 && (e = 13)) : e = t, e === 10 && (e = 13), 32 <= e || e === 13 ? e : 0;
	}
	function Vn() {
		return !0;
	}
	function Hn() {
		return !1;
	}
	function Un(e) {
		function t(t, n, r, i, a) {
			for (var o in this._reactName = t, this._targetInst = r, this.type = n, this.nativeEvent = i, this.target = a, this.currentTarget = null, e) e.hasOwnProperty(o) && (t = e[o], this[o] = t ? t(i) : i[o]);
			return this.isDefaultPrevented = (i.defaultPrevented == null ? !1 === i.returnValue : i.defaultPrevented) ? Vn : Hn, this.isPropagationStopped = Hn, this;
		}
		return E(t.prototype, {
			preventDefault: function() {
				this.defaultPrevented = !0;
				var e = this.nativeEvent;
				e && (e.preventDefault ? e.preventDefault() : typeof e.returnValue != "unknown" && (e.returnValue = !1), this.isDefaultPrevented = Vn);
			},
			stopPropagation: function() {
				var e = this.nativeEvent;
				e && (e.stopPropagation ? e.stopPropagation() : typeof e.cancelBubble != "unknown" && (e.cancelBubble = !0), this.isPropagationStopped = Vn);
			},
			persist: function() {},
			isPersistent: Vn
		}), t;
	}
	var Wn = {
		eventPhase: 0,
		bubbles: 0,
		cancelable: 0,
		timeStamp: function(e) {
			return e.timeStamp || Date.now();
		},
		defaultPrevented: 0,
		isTrusted: 0
	}, Gn = Un(Wn), Kn = E({}, Wn, {
		view: 0,
		detail: 0
	}), qn = Un(Kn), Jn, Yn, Xn, Zn = E({}, Kn, {
		screenX: 0,
		screenY: 0,
		clientX: 0,
		clientY: 0,
		pageX: 0,
		pageY: 0,
		ctrlKey: 0,
		shiftKey: 0,
		altKey: 0,
		metaKey: 0,
		getModifierState: cr,
		button: 0,
		buttons: 0,
		relatedTarget: function(e) {
			return e.relatedTarget === void 0 ? e.fromElement === e.srcElement ? e.toElement : e.fromElement : e.relatedTarget;
		},
		movementX: function(e) {
			return "movementX" in e ? e.movementX : (e !== Xn && (Xn && e.type === "mousemove" ? (Jn = e.screenX - Xn.screenX, Yn = e.screenY - Xn.screenY) : Yn = Jn = 0, Xn = e), Jn);
		},
		movementY: function(e) {
			return "movementY" in e ? e.movementY : Yn;
		}
	}), Qn = Un(Zn), $n = Un(E({}, Zn, { dataTransfer: 0 })), er = Un(E({}, Kn, { relatedTarget: 0 })), tr = Un(E({}, Wn, {
		animationName: 0,
		elapsedTime: 0,
		pseudoElement: 0
	})), nr = Un(E({}, Wn, { clipboardData: function(e) {
		return "clipboardData" in e ? e.clipboardData : window.clipboardData;
	} })), rr = Un(E({}, Wn, { data: 0 })), ir = {
		Esc: "Escape",
		Spacebar: " ",
		Left: "ArrowLeft",
		Up: "ArrowUp",
		Right: "ArrowRight",
		Down: "ArrowDown",
		Del: "Delete",
		Win: "OS",
		Menu: "ContextMenu",
		Apps: "ContextMenu",
		Scroll: "ScrollLock",
		MozPrintableKey: "Unidentified"
	}, ar = {
		8: "Backspace",
		9: "Tab",
		12: "Clear",
		13: "Enter",
		16: "Shift",
		17: "Control",
		18: "Alt",
		19: "Pause",
		20: "CapsLock",
		27: "Escape",
		32: " ",
		33: "PageUp",
		34: "PageDown",
		35: "End",
		36: "Home",
		37: "ArrowLeft",
		38: "ArrowUp",
		39: "ArrowRight",
		40: "ArrowDown",
		45: "Insert",
		46: "Delete",
		112: "F1",
		113: "F2",
		114: "F3",
		115: "F4",
		116: "F5",
		117: "F6",
		118: "F7",
		119: "F8",
		120: "F9",
		121: "F10",
		122: "F11",
		123: "F12",
		144: "NumLock",
		145: "ScrollLock",
		224: "Meta"
	}, or = {
		Alt: "altKey",
		Control: "ctrlKey",
		Meta: "metaKey",
		Shift: "shiftKey"
	};
	function sr(e) {
		var t = this.nativeEvent;
		return t.getModifierState ? t.getModifierState(e) : (e = or[e]) ? !!t[e] : !1;
	}
	function cr() {
		return sr;
	}
	var lr = Un(E({}, Kn, {
		key: function(e) {
			if (e.key) {
				var t = ir[e.key] || e.key;
				if (t !== "Unidentified") return t;
			}
			return e.type === "keypress" ? (e = Bn(e), e === 13 ? "Enter" : String.fromCharCode(e)) : e.type === "keydown" || e.type === "keyup" ? ar[e.keyCode] || "Unidentified" : "";
		},
		code: 0,
		location: 0,
		ctrlKey: 0,
		shiftKey: 0,
		altKey: 0,
		metaKey: 0,
		repeat: 0,
		locale: 0,
		getModifierState: cr,
		charCode: function(e) {
			return e.type === "keypress" ? Bn(e) : 0;
		},
		keyCode: function(e) {
			return e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
		},
		which: function(e) {
			return e.type === "keypress" ? Bn(e) : e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
		}
	})), ur = Un(E({}, Zn, {
		pointerId: 0,
		width: 0,
		height: 0,
		pressure: 0,
		tangentialPressure: 0,
		tiltX: 0,
		tiltY: 0,
		twist: 0,
		pointerType: 0,
		isPrimary: 0
	})), dr = Un(E({}, Wn, { submitter: 0 })), fr = Un(E({}, Kn, {
		touches: 0,
		targetTouches: 0,
		changedTouches: 0,
		altKey: 0,
		metaKey: 0,
		ctrlKey: 0,
		shiftKey: 0,
		getModifierState: cr
	})), pr = Un(E({}, Wn, {
		propertyName: 0,
		elapsedTime: 0,
		pseudoElement: 0
	})), mr = Un(E({}, Zn, {
		deltaX: function(e) {
			return "deltaX" in e ? e.deltaX : "wheelDeltaX" in e ? -e.wheelDeltaX : 0;
		},
		deltaY: function(e) {
			return "deltaY" in e ? e.deltaY : "wheelDeltaY" in e ? -e.wheelDeltaY : "wheelDelta" in e ? -e.wheelDelta : 0;
		},
		deltaZ: 0,
		deltaMode: 0
	})), hr = Un(E({}, Wn, {
		newState: 0,
		oldState: 0,
		source: 0
	})), gr = [
		9,
		13,
		27,
		32
	], _r = Nn && "CompositionEvent" in window, vr = null;
	Nn && "documentMode" in document && (vr = document.documentMode);
	var yr = Nn && "TextEvent" in window && !vr, br = Nn && (!_r || vr && 8 < vr && 11 >= vr), xr = " ", Sr = !1;
	function Cr(e, t) {
		switch (e) {
			case "keyup": return gr.indexOf(t.keyCode) !== -1;
			case "keydown": return t.keyCode !== 229;
			case "keypress":
			case "mousedown":
			case "focusout": return !0;
			default: return !1;
		}
	}
	function wr(e) {
		return e = e.detail, typeof e == "object" && "data" in e ? e.data : null;
	}
	var Tr = !1;
	function Er(e, t) {
		switch (e) {
			case "compositionend": return wr(t);
			case "keypress": return t.which === 32 ? (Sr = !0, xr) : null;
			case "textInput": return e = t.data, e === xr && Sr ? null : e;
			default: return null;
		}
	}
	function Dr(e, t) {
		if (Tr) return e === "compositionend" || !_r && Cr(e, t) ? (e = zn(), Rn = Ln = In = null, Tr = !1, e) : null;
		switch (e) {
			case "paste": return null;
			case "keypress":
				if (!(t.ctrlKey || t.altKey || t.metaKey) || t.ctrlKey && t.altKey) {
					if (t.char && 1 < t.char.length) return t.char;
					if (t.which) return String.fromCharCode(t.which);
				}
				return null;
			case "compositionend": return br && t.locale !== "ko" ? null : t.data;
			default: return null;
		}
	}
	var Or = {
		color: !0,
		date: !0,
		datetime: !0,
		"datetime-local": !0,
		email: !0,
		month: !0,
		number: !0,
		password: !0,
		range: !0,
		search: !0,
		tel: !0,
		text: !0,
		time: !0,
		url: !0,
		week: !0
	};
	function kr(e) {
		var t = e && e.nodeName && e.nodeName.toLowerCase();
		return t === "input" ? !!Or[e.type] : t === "textarea";
	}
	function Ar(e, t, n, r) {
		Dn ? On ? On.push(r) : On = [r] : Dn = r, t = Jf(t, "onChange"), 0 < t.length && (n = new Gn("onChange", "change", null, n, r), e.push({
			event: n,
			listeners: t
		}));
	}
	var jr = null, Mr = null;
	function Nr(e) {
		Vf(e, 0);
	}
	function Pr(e) {
		if (sn(Bt(e))) return e;
	}
	function Fr(e, t) {
		if (e === "change") return t;
	}
	var Ir = !1;
	if (Nn) {
		var Lr;
		if (Nn) {
			var Rr = "oninput" in document;
			if (!Rr) {
				var zr = document.createElement("div");
				zr.setAttribute("oninput", "return;"), Rr = typeof zr.oninput == "function";
			}
			Lr = Rr;
		} else Lr = !1;
		Ir = Lr && (!document.documentMode || 9 < document.documentMode);
	}
	function Br() {
		jr && (jr.detachEvent("onpropertychange", Vr), Mr = jr = null);
	}
	function Vr(e) {
		if (e.propertyName === "value" && Pr(Mr)) {
			var t = [];
			Ar(t, Mr, e, En(e)), jn(Nr, t);
		}
	}
	function Hr(e, t, n) {
		e === "focusin" ? (Br(), jr = t, Mr = n, jr.attachEvent("onpropertychange", Vr)) : e === "focusout" && Br();
	}
	function Ur(e) {
		if (e === "selectionchange" || e === "keyup" || e === "keydown") return Pr(Mr);
	}
	function Wr(e, t) {
		if (e === "click") return Pr(t);
	}
	function Gr(e, t) {
		if (e === "input" || e === "change") return Pr(t);
	}
	function Kr(e, t) {
		return e === t && (e !== 0 || 1 / e == 1 / t) || e !== e && t !== t;
	}
	var qr = typeof Object.is == "function" ? Object.is : Kr;
	function Jr(e, t) {
		if (qr(e, t)) return !0;
		if (typeof e != "object" || !e || typeof t != "object" || !t) return !1;
		var n = Object.keys(e), r = Object.keys(t);
		if (n.length !== r.length) return !1;
		for (r = 0; r < n.length; r++) {
			var i = n[r];
			if (!He.call(t, i) || !qr(e[i], t[i])) return !1;
		}
		return !0;
	}
	function Yr(e) {
		if (e ||= typeof document < "u" ? document : void 0, e === void 0) return null;
		try {
			return e.activeElement || e.body;
		} catch {
			return e.body;
		}
	}
	function Xr(e) {
		for (; e && e.firstChild;) e = e.firstChild;
		return e;
	}
	function Zr(e, t) {
		var n = Xr(e);
		e = 0;
		for (var r; n;) {
			if (n.nodeType === 3) {
				if (r = e + n.textContent.length, e <= t && r >= t) return {
					node: n,
					offset: t - e
				};
				e = r;
			}
			a: {
				for (; n;) {
					if (n.nextSibling) {
						n = n.nextSibling;
						break a;
					}
					n = n.parentNode;
				}
				n = void 0;
			}
			n = Xr(n);
		}
	}
	function Qr(e, t) {
		return e && t ? e === t ? !0 : e && e.nodeType === 3 ? !1 : t && t.nodeType === 3 ? Qr(e, t.parentNode) : "contains" in e ? e.contains(t) : e.compareDocumentPosition ? !!(e.compareDocumentPosition(t) & 16) : !1 : !1;
	}
	function $r(e) {
		e = e != null && e.ownerDocument != null && e.ownerDocument.defaultView != null ? e.ownerDocument.defaultView : window;
		for (var t = Yr(e.document); t instanceof e.HTMLIFrameElement;) {
			try {
				var n = typeof t.contentWindow.location.href == "string";
			} catch {
				n = !1;
			}
			if (n) e = t.contentWindow;
			else break;
			t = Yr(e.document);
		}
		return t;
	}
	function ei(e) {
		var t = e && e.nodeName && e.nodeName.toLowerCase();
		return t && (t === "input" && (e.type === "text" || e.type === "search" || e.type === "tel" || e.type === "url" || e.type === "password") || t === "textarea" || e.contentEditable === "true");
	}
	var ti = Nn && "documentMode" in document && 11 >= document.documentMode, ni = null, ri = null, ii = null, ai = !1;
	function oi(e, t, n) {
		var r = n.window === n ? n.document : n.nodeType === 9 ? n : n.ownerDocument;
		ai || ni == null || ni !== Yr(r) || (r = ni, "selectionStart" in r && ei(r) ? r = {
			start: r.selectionStart,
			end: r.selectionEnd
		} : (r = (r.ownerDocument && r.ownerDocument.defaultView || window).getSelection(), r = {
			anchorNode: r.anchorNode,
			anchorOffset: r.anchorOffset,
			focusNode: r.focusNode,
			focusOffset: r.focusOffset
		}), ii && Jr(ii, r) || (ii = r, r = Jf(ri, "onSelect"), 0 < r.length && (t = new Gn("onSelect", "select", null, t, n), e.push({
			event: t,
			listeners: r
		}), t.target = ni)));
	}
	function si(e, t) {
		var n = {};
		return n[e.toLowerCase()] = t.toLowerCase(), n["Webkit" + e] = "webkit" + t, n["Moz" + e] = "moz" + t, n;
	}
	var ci = {
		animationend: si("Animation", "AnimationEnd"),
		animationiteration: si("Animation", "AnimationIteration"),
		animationstart: si("Animation", "AnimationStart"),
		transitionrun: si("Transition", "TransitionRun"),
		transitionstart: si("Transition", "TransitionStart"),
		transitioncancel: si("Transition", "TransitionCancel"),
		transitionend: si("Transition", "TransitionEnd")
	}, li = {}, ui = {};
	Nn && (ui = document.createElement("div").style, "AnimationEvent" in window || (delete ci.animationend.animation, delete ci.animationiteration.animation, delete ci.animationstart.animation), "TransitionEvent" in window || delete ci.transitionend.transition);
	function di(e) {
		if (li[e]) return li[e];
		if (!ci[e]) return e;
		var t = ci[e], n;
		for (n in t) if (t.hasOwnProperty(n) && n in ui) return li[e] = t[n];
		return e;
	}
	var fi = di("animationend"), pi = di("animationiteration"), mi = di("animationstart"), hi = di("transitionrun"), gi = di("transitionstart"), _i = di("transitioncancel"), vi = di("transitionend"), yi = /* @__PURE__ */ new Map(), bi = "abort auxClick beforeToggle cancel canPlay canPlayThrough click close contextMenu copy cut drag dragEnd dragEnter dragExit dragLeave dragOver dragStart drop durationChange emptied encrypted ended error fullscreenChange fullscreenError gotPointerCapture input invalid keyDown keyPress keyUp load loadedData loadedMetadata loadStart lostPointerCapture mouseDown mouseMove mouseOut mouseOver mouseUp paste pause play playing pointerCancel pointerDown pointerMove pointerOut pointerOver pointerUp progress rateChange reset resize seeked seeking stalled submit suspend timeUpdate touchCancel touchEnd touchStart volumeChange scroll toggle touchMove waiting wheel".split(" ");
	bi.push("scrollEnd");
	function xi(e, t) {
		yi.set(e, t), Kt(t, [e]);
	}
	var Si = 0;
	function Ci(e, t) {
		if (e.name != null && e.name !== "auto") return e.name;
		if (t.autoName !== null) return t.autoName;
		e = bd.identifierPrefix;
		var n = Si++;
		return e = "_" + e + "t_" + n.toString(32) + "_", t.autoName = e;
	}
	function wi(e) {
		if (e == null || typeof e == "string") return e;
		var t = null, n = Od;
		if (n !== null) for (var r = 0; r < n.length; r++) {
			var i = e[n[r]];
			if (i != null) {
				if (i === "none") return "none";
				t = t == null ? i : t + (" " + i);
			}
		}
		return t ?? e.default;
	}
	function Ti(e, t) {
		return e = wi(e), t = wi(t), t == null ? e === "auto" ? null : e : t === "auto" ? null : t;
	}
	var Ei = typeof reportError == "function" ? reportError : function(e) {
		if (typeof window == "object" && typeof window.ErrorEvent == "function") {
			var t = new window.ErrorEvent("error", {
				bubbles: !0,
				cancelable: !0,
				message: typeof e == "object" && e && typeof e.message == "string" ? String(e.message) : String(e),
				error: e
			});
			if (!window.dispatchEvent(t)) return;
		} else if (typeof process == "object" && typeof process.emit == "function") {
			process.emit("uncaughtException", e);
			return;
		}
		console.error(e);
	}, Di = [], Oi = 0, ki = 0;
	function Ai() {
		for (var e = Oi, t = ki = Oi = 0; t < e;) {
			var n = Di[t];
			Di[t++] = null;
			var r = Di[t];
			Di[t++] = null;
			var i = Di[t];
			Di[t++] = null;
			var a = Di[t];
			if (Di[t++] = null, r !== null && i !== null) {
				var o = r.pending;
				o === null ? i.next = i : (i.next = o.next, o.next = i), r.pending = i;
			}
			a !== 0 && Pi(n, i, a);
		}
	}
	function ji(e, t, n, r) {
		Di[Oi++] = e, Di[Oi++] = t, Di[Oi++] = n, Di[Oi++] = r, ki |= r, e.lanes |= r, e = e.alternate, e !== null && (e.lanes |= r);
	}
	function Mi(e, t, n, r) {
		return ji(e, t, n, r), Fi(e);
	}
	function Ni(e, t) {
		return ji(e, null, null, t), Fi(e);
	}
	function Pi(e, t, n) {
		e.lanes |= n;
		var r = e.alternate;
		r !== null && (r.lanes |= n);
		for (var i = !1, a = e.return; a !== null;) a.childLanes |= n, r = a.alternate, r !== null && (r.childLanes |= n), a.tag === 22 && (e = a.stateNode, e === null || e._visibility & 1 || (i = !0)), e = a, a = a.return;
		return e.tag === 3 ? (a = e.stateNode, i && t !== null && (i = 31 - at(n), e = a.hiddenUpdates, r = e[i], r === null ? e[i] = [t] : r.push(t), t.lane = n | 536870912), a) : null;
	}
	function Fi(e) {
		if (50 < kd) throw kd = 0, Ad = null, Error(i(185));
		for (var t = e.return; t !== null;) e = t, t = e.return;
		return e.tag === 3 ? e.stateNode : null;
	}
	var Ii = {};
	function Li(e, t, n, r) {
		this.tag = e, this.key = n, this.sibling = this.child = this.return = this.stateNode = this.type = this.elementType = null, this.index = 0, this.refCleanup = this.ref = null, this.pendingProps = t, this.dependencies = this.memoizedState = this.updateQueue = this.memoizedProps = null, this.mode = r, this.subtreeFlags = this.flags = 0, this.deletions = null, this.childLanes = this.lanes = 0, this.alternate = null;
	}
	function Ri(e, t, n, r) {
		return new Li(e, t, n, r);
	}
	function zi(e) {
		return e = e.prototype, !(!e || !e.isReactComponent);
	}
	function Bi(e, t) {
		var n = e.alternate;
		return n === null ? (n = Ri(e.tag, t, e.key, e.mode), n.elementType = e.elementType, n.type = e.type, n.stateNode = e.stateNode, n.alternate = e, e.alternate = n) : (n.pendingProps = t, n.type = e.type, n.flags = 0, n.subtreeFlags = 0, n.deletions = null), n.flags = e.flags & 1206910976, n.childLanes = e.childLanes, n.lanes = e.lanes, n.child = e.child, n.memoizedProps = e.memoizedProps, n.memoizedState = e.memoizedState, n.updateQueue = e.updateQueue, t = e.dependencies, n.dependencies = t === null ? null : {
			lanes: t.lanes,
			firstContext: t.firstContext
		}, n.sibling = e.sibling, n.index = e.index, n.ref = e.ref, n.refCleanup = e.refCleanup, n;
	}
	function Vi(e, t) {
		e.flags &= 1206910978;
		var n = e.alternate;
		return n === null ? (e.childLanes = 0, e.lanes = t, e.child = null, e.subtreeFlags = 0, e.memoizedProps = null, e.memoizedState = null, e.updateQueue = null, e.dependencies = null, e.stateNode = null) : (e.childLanes = n.childLanes, e.lanes = n.lanes, e.child = n.child, e.subtreeFlags = 0, e.deletions = null, e.memoizedProps = n.memoizedProps, e.memoizedState = n.memoizedState, e.updateQueue = n.updateQueue, e.type = n.type, t = n.dependencies, e.dependencies = t === null ? null : {
			lanes: t.lanes,
			firstContext: t.firstContext
		}), e;
	}
	function Hi(e, t, n, r, a, o) {
		var s = 0;
		if (r = e, typeof r == "function") zi(r) && (s = 1);
		else if (typeof r == "string") s = qm(e, n, De.current) ? 26 : e === "html" || e === "head" || e === "body" ? 27 : 5;
		else a: switch (r) {
			case pe: return e = Ri(31, n, t, a), e.elementType = pe, e.lanes = o, e;
			case D: return Ui(n.children, a, o, t);
			case ie:
				s = 8, a |= 24;
				break;
			case ae: return e = Ri(12, n, t, a | 2), e.elementType = ae, e.lanes = o, e;
			case le: return e = Ri(13, n, t, a), e.elementType = le, e.lanes = o, e;
			case ue: return e = Ri(19, n, t, a), e.elementType = ue, e.lanes = o, e;
			case me:
			case O: return e = a | 32, e = Ri(30, n, t, e), e.elementType = O, e.lanes = o, e.stateNode = {
				autoName: null,
				paired: null,
				clones: null,
				ref: null
			}, e;
			default:
				if (typeof r == "object" && r) switch (r.$$typeof) {
					case se:
						s = 10;
						break a;
					case oe:
						s = 9;
						break a;
					case ce:
						s = 11;
						break a;
					case de:
						s = 14;
						break a;
					case fe:
						s = 16, r = null;
						break a;
				}
				s = 29, n = Error(i(130, e === null ? "null" : typeof e, "")), r = null;
		}
		return t = Ri(s, n, t, a), t.elementType = e, t.type = r, t.lanes = o, t;
	}
	function Ui(e, t, n, r) {
		return e = Ri(7, e, r, t), e.lanes = n, e;
	}
	function Wi(e, t, n) {
		return e = Ri(6, e, null, t), e.lanes = n, e;
	}
	function Gi(e) {
		var t = Ri(18, null, null, 0);
		return t.stateNode = e, t;
	}
	function Ki(e, t, n) {
		return t = Ri(4, e.children === null ? [] : e.children, e.key, t), t.lanes = n, t.stateNode = {
			containerInfo: e.containerInfo,
			pendingChildren: null,
			implementation: e.implementation
		}, t;
	}
	var qi = /* @__PURE__ */ new WeakMap();
	function Ji(e, t) {
		if (typeof e == "object" && e) {
			var n = qi.get(e);
			return n === void 0 ? (t = {
				value: e,
				source: t,
				stack: Ve(t)
			}, qi.set(e, t), t) : n;
		}
		return {
			value: e,
			source: t,
			stack: Ve(t)
		};
	}
	var Yi = [], Xi = 0, Zi = null, Qi = 0, $i = [], ea = 0, ta = null, na = 1, ra = "";
	function ia(e, t) {
		Yi[Xi++] = Qi, Yi[Xi++] = Zi, Zi = e, Qi = t;
	}
	function aa(e, t, n) {
		$i[ea++] = na, $i[ea++] = ra, $i[ea++] = ta, ta = e;
		var r = na;
		e = ra;
		var i = 32 - at(r) - 1;
		r &= ~(1 << i), n += 1;
		var a = 32 - at(t) + i;
		if (30 < a) {
			var o = i - i % 5;
			a = (r & (1 << o) - 1).toString(32), r >>= o, i -= o, na = 1 << 32 - at(t) + i | n << i | r, ra = a + e;
		} else na = 1 << a | n << i | r, ra = e;
	}
	function oa(e) {
		e.return !== null && (ia(e, 1), aa(e, 1, 0));
	}
	function sa(e) {
		for (; e === Zi;) Zi = Yi[--Xi], Yi[Xi] = null, Qi = Yi[--Xi], Yi[Xi] = null;
		for (; e === ta;) ta = $i[--ea], $i[ea] = null, ra = $i[--ea], $i[ea] = null, na = $i[--ea], $i[ea] = null;
	}
	function ca(e, t) {
		$i[ea++] = na, $i[ea++] = ra, $i[ea++] = ta, na = t.id, ra = t.overflow, ta = e;
	}
	var la = null, P = null, F = !1, ua = null, da = !1, fa = Error(i(519));
	function pa(e) {
		throw ya(Ji(Error(i(418, 1 < arguments.length && arguments[1] !== void 0 && arguments[1] ? "text" : "HTML", "")), e)), fa;
	}
	function ma(e) {
		var t = e.stateNode, n = e.type, r = e.memoizedProps;
		switch (t[Ot] = e, t[kt] = r, n) {
			case "dialog":
				Q("cancel", t), Q("close", t);
				break;
			case "iframe":
			case "object":
			case "embed":
				Q("load", t);
				break;
			case "video":
			case "audio":
				for (n = 0; n < zf.length; n++) Q(zf[n], t);
				break;
			case "source":
				Q("error", t);
				break;
			case "img":
			case "image":
			case "link":
				Q("error", t), Q("load", t);
				break;
			case "details":
				Q("toggle", t);
				break;
			case "input":
				Q("invalid", t), dn(t, r.value, r.defaultValue, r.checked, r.defaultChecked, r.type, r.name, !0);
				break;
			case "select":
				Q("invalid", t);
				break;
			case "textarea": Q("invalid", t), hn(t, r.value, r.defaultValue, r.children);
		}
		n = r.children, typeof n != "string" && typeof n != "number" && typeof n != "bigint" || t.textContent === "" + n || !0 === r.suppressHydrationWarning || ep(t.textContent, n) ? (r.popover != null && (Q("beforetoggle", t), Q("toggle", t)), r.onScroll != null && Q("scroll", t), r.onScrollEnd != null && Q("scrollend", t), r.onClick != null && (t.onclick = wn), t = !0) : t = !1, t || pa(e, !0);
	}
	function ha(e) {
		for (la = e.return; la;) switch (la.tag) {
			case 5:
			case 31:
			case 13:
				da = !1;
				return;
			case 27:
			case 3:
				da = !0;
				return;
			default: la = la.return;
		}
	}
	function ga(e) {
		if (e !== la) return !1;
		if (!F) return ha(e), F = !0, !1;
		var t = e.tag, n;
		if ((n = t !== 3 && t !== 27) && ((n = t === 5) && (n = e.type, n = n === "form" || n === "button" || pp(e.type, e.memoizedProps)), n = !n), n && P && pa(e), ha(e), t === 13) {
			if (e = e.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(317));
			P = dm(e);
		} else if (t === 31) {
			if (e = e.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(317));
			P = dm(e);
		} else t === 27 ? (t = P, Sp(e.type) ? (e = um, um = null, P = e) : P = t) : P = la ? lm(e.stateNode.nextSibling) : null;
		return !0;
	}
	function _a() {
		P = la = null, F = !1;
	}
	function va() {
		var e = ua;
		return e !== null && (pd === null ? pd = e : pd.push.apply(pd, e), ua = null), e;
	}
	function ya(e) {
		ua === null ? ua = [e] : ua.push(e);
	}
	var ba = Te(null), xa = null, Sa = null;
	function Ca(e, t, n) {
		j(ba, t._currentValue), t._currentValue = n;
	}
	function wa(e) {
		e._currentValue = ba.current, Ee(ba);
	}
	function Ta(e, t, n) {
		for (; e !== null;) {
			var r = e.alternate;
			if ((e.childLanes & t) === t ? r !== null && (r.childLanes & t) !== t && (r.childLanes |= t) : (e.childLanes |= t, r !== null && (r.childLanes |= t)), e === n) break;
			e = e.return;
		}
	}
	function Ea(e, t, n, r) {
		var a = e.child;
		for (a !== null && (a.return = e); a !== null;) {
			var o = a.dependencies;
			if (o !== null) {
				var s = a.child;
				o = o.firstContext;
				a: for (; o !== null;) {
					var c = o;
					o = a;
					for (var l = 0; l < t.length; l++) if (c.context === t[l]) {
						o.lanes |= n, c = o.alternate, c !== null && (c.lanes |= n), Ta(o.return, n, e), r || (s = null);
						break a;
					}
					o = c.next;
				}
			} else if (a.tag === 18) {
				if (s = a.return, s === null) throw Error(i(341));
				s.lanes |= n, o = s.alternate, o !== null && (o.lanes |= n), Ta(s, n, e), s = null;
			} else a.tag === 13 && a.memoizedState !== null && a.memoizedState.dehydrated === null ? (a.lanes |= n, s = a.alternate, s !== null && (s.lanes |= n), Ta(a.return, n, e), s = a.child, s = s === null ? null : s.sibling) : s = a.child;
			if (s !== null) s.return = a;
			else for (s = a; s !== null;) {
				if (s === e) {
					s = null;
					break;
				}
				if (a = s.sibling, a !== null) {
					a.return = s.return, s = a;
					break;
				}
				s = s.return;
			}
			a = s;
		}
	}
	function Da(e, t, n, r) {
		e = null;
		for (var a = t, o = !1; a !== null;) {
			if (!o) {
				if (a.flags & 524288) o = !0;
				else if (a.flags & 262144) break;
			}
			if (a.tag === 10) {
				var s = a.alternate;
				if (s === null) throw Error(i(387));
				if (s = s.memoizedProps, s !== null) {
					var c = a.type;
					qr(a.pendingProps.value, s.value) || (e === null ? e = [c] : e.push(c));
				}
			} else if (a === Ae.current) {
				if (s = a.alternate, s === null) throw Error(i(387));
				s.memoizedState.memoizedState !== a.memoizedState.memoizedState && (e === null ? e = [sh] : e.push(sh));
			}
			a = a.return;
		}
		return e !== null && Ea(t, e, n, r), t.flags |= 262144, e !== null;
	}
	function Oa(e) {
		for (e = e.firstContext; e !== null;) {
			if (!qr(e.context._currentValue, e.memoizedValue)) return !0;
			e = e.next;
		}
		return !1;
	}
	function ka(e) {
		xa = e, Sa = null, e = e.dependencies, e !== null && (e.firstContext = null);
	}
	function Aa(e) {
		return Ma(xa, e);
	}
	function ja(e, t) {
		return xa === null && ka(e), Ma(e, t);
	}
	function Ma(e, t) {
		var n = t._currentValue;
		if (t = {
			context: t,
			memoizedValue: n,
			next: null
		}, Sa === null) {
			if (e === null) throw Error(i(308));
			Sa = t, e.dependencies = {
				lanes: 0,
				firstContext: t
			}, e.flags |= 524288;
		} else Sa = Sa.next = t;
		return n;
	}
	var Na = typeof AbortController < "u" ? AbortController : function() {
		var e = [], t = this.signal = {
			aborted: !1,
			addEventListener: function(t, n) {
				e.push(n);
			}
		};
		this.abort = function() {
			t.aborted = !0, e.forEach(function(e) {
				return e();
			});
		};
	}, Pa = t.unstable_scheduleCallback, Fa = t.unstable_NormalPriority, Ia = {
		$$typeof: se,
		Consumer: null,
		Provider: null,
		_currentValue: null,
		_currentValue2: null,
		_threadCount: 0
	};
	function La() {
		return {
			controller: new Na(),
			data: /* @__PURE__ */ new Map(),
			refCount: 0
		};
	}
	function Ra(e) {
		e.refCount--, e.refCount === 0 && Pa(Fa, function() {
			e.controller.abort();
		});
	}
	function za(e, t) {
		if (e.pendingLanes & 4194048) {
			var n = e.transitionTypes;
			for (n === null && (n = e.transitionTypes = []), e = 0; e < t.length; e++) {
				var r = t[e];
				n.indexOf(r) === -1 && n.push(r);
			}
		}
	}
	var Ba = null;
	function Va(e) {
		var t = e.transitionTypes;
		return e.transitionTypes = null, t;
	}
	var Ha = null, Ua = 0, Wa = 0, Ga = null;
	function Ka(e, t) {
		if (Ha === null) {
			var n = Ha = [];
			Ua = 0, Wa = Pf(), Ga = {
				status: "pending",
				value: void 0,
				then: function(e) {
					n.push(e);
				}
			};
		}
		return Ua++, t.then(qa, qa), t;
	}
	function qa() {
		if (--Ua === 0 && (Ba = null, Ha !== null)) {
			Ga !== null && (Ga.status = "fulfilled");
			var e = Ha;
			Ha = null, Wa = 0, Ga = null;
			for (var t = 0; t < e.length; t++) (0, e[t])();
		}
	}
	function Ja(e, t) {
		var n = [], r = {
			status: "pending",
			value: null,
			reason: null,
			then: function(e) {
				n.push(e);
			}
		};
		return e.then(function() {
			r.status = "fulfilled", r.value = t;
			for (var e = 0; e < n.length; e++) (0, n[e])(t);
		}, function(e) {
			for (r.status = "rejected", r.reason = e, e = 0; e < n.length; e++) (0, n[e])(void 0);
		}), r;
	}
	var Ya = k.S;
	k.S = function(e, t) {
		if (gd = qe(), typeof t == "object" && t && typeof t.then == "function" && Ka(e, t), Ba !== null) for (var n = bf; n !== null;) za(n, Ba), n = n.next;
		if (n = e.types, n !== null) {
			for (var r = bf; r !== null;) za(r, n), r = r.next;
			if (Wa !== 0) {
				r = Ba, r === null && (r = Ba = []);
				for (var i = 0; i < n.length; i++) {
					var a = n[i];
					r.indexOf(a) === -1 && r.push(a);
				}
			}
		}
		Ya !== null && Ya(e, t);
	};
	var Xa = Te(null);
	function Za() {
		var e = Xa.current;
		return e === null ? G.pooledCache : e;
	}
	function Qa(e, t) {
		t === null ? j(Xa, Xa.current) : j(Xa, t.pool);
	}
	function $a() {
		var e = Za();
		return e === null ? null : {
			parent: Ia._currentValue,
			pool: e
		};
	}
	var eo = Error(i(460)), to = Error(i(474)), no = Error(i(542)), ro = { then: function() {} };
	function io(e) {
		return e = e.status, e === "fulfilled" || e === "rejected";
	}
	function ao(e, t, n) {
		switch (n = e[n], n === void 0 ? e.push(t) : n !== t && (t.then(wn, wn), t = n), t.status) {
			case "fulfilled": return t.value;
			case "rejected": throw e = t.reason, lo(e), e === void 0 && !("reason" in t) ? Error(i(600)) : e;
			default:
				if (typeof t.status == "string") t.then(wn, wn);
				else {
					if (e = G, e !== null && 100 < e.shellSuspendCounter) throw Error(i(482));
					e = t, e.status = "pending", e.then(function(e) {
						if (t.status === "pending") {
							var n = t;
							n.status = "fulfilled", n.value = e;
						}
					}, function(e) {
						if (t.status === "pending") {
							var n = t;
							n.status = "rejected", n.reason = e;
						}
					});
				}
				switch (t.status) {
					case "fulfilled": return t.value;
					case "rejected": throw e = t.reason, lo(e), e;
				}
				throw so = t, eo;
		}
	}
	function oo(e) {
		try {
			var t = e._init;
			return t(e._payload);
		} catch (e) {
			throw typeof e == "object" && e && typeof e.then == "function" ? (so = e, eo) : e;
		}
	}
	var so = null;
	function co() {
		if (so === null) throw Error(i(459));
		var e = so;
		return so = null, e;
	}
	function lo(e) {
		if (e === eo || e === no) throw Error(i(483));
	}
	var uo = null, fo = 0;
	function po(e) {
		var t = fo;
		return fo += 1, uo === null && (uo = []), ao(uo, e, t);
	}
	function mo(e, t) {
		t = t.props.ref, e.ref = t === void 0 ? null : t;
	}
	function ho(e, t) {
		throw t.$$typeof === te ? Error(i(525)) : (e = Object.prototype.toString.call(t), Error(i(31, e === "[object Object]" ? "object with keys {" + Object.keys(t).join(", ") + "}" : e)));
	}
	function go(e) {
		function t(t, n) {
			if (e) {
				var r = t.deletions;
				r === null ? (t.deletions = [n], t.flags |= 16) : r.push(n);
			}
		}
		function n(n, r) {
			if (!e) return null;
			for (; r !== null;) t(n, r), r = r.sibling;
			return null;
		}
		function r(e) {
			for (var t = /* @__PURE__ */ new Map(); e !== null;) e.key === null ? t.set(e.index, e) : t.set(e.key, e), e = e.sibling;
			return t;
		}
		function a(e, t) {
			return e = Bi(e, t), e.index = 0, e.sibling = null, e;
		}
		function o(t, n, r) {
			return t.index = r, e ? (r = t.alternate, r === null ? (t.flags |= 134217730, n) : (r = r.index, r < n ? (t.flags |= 2, n) : r)) : (t.flags |= 1048576, n);
		}
		function s(t) {
			return e && t.alternate === null && (t.flags |= 134217730), t;
		}
		function c(e, t, n, r) {
			return t === null || t.tag !== 6 ? (t = Wi(n, e.mode, r), t.return = e, t) : (t = a(t, n), t.return = e, t);
		}
		function l(e, t, n, r) {
			var i = n.type;
			return i === D ? (e = d(e, t, n.props.children, r, n.key), mo(e, n), e) : t !== null && (t.elementType === i || typeof i == "object" && i && i.$$typeof === fe && oo(i) === t.type) ? (t = a(t, n.props), mo(t, n), t.return = e, t) : (t = Hi(n.type, n.key, n.props, null, e.mode, r), mo(t, n), t.return = e, t);
		}
		function u(e, t, n, r) {
			return t === null || t.tag !== 4 || t.stateNode.containerInfo !== n.containerInfo || t.stateNode.implementation !== n.implementation ? (t = Ki(n, e.mode, r), t.return = e, t) : (t = a(t, n.children || []), t.return = e, t);
		}
		function d(e, t, n, r, i) {
			return t === null || t.tag !== 7 ? (t = Ui(n, e.mode, r, i), t.return = e, t) : (t = a(t, n), t.return = e, t);
		}
		function f(e, t, n) {
			if (typeof t == "string" && t !== "" || typeof t == "number" || typeof t == "bigint") return t = Wi("" + t, e.mode, n), t.return = e, t;
			if (typeof t == "object" && t) {
				switch (t.$$typeof) {
					case ne: return n = Hi(t.type, t.key, t.props, null, e.mode, n), mo(n, t), n.return = e, n;
					case re: return t = Ki(t, e.mode, n), t.return = e, t;
					case fe: return t = oo(t), f(e, t, n);
				}
				if (xe(t) || ve(t)) return t = Ui(t, e.mode, n, null), t.return = e, t;
				if (typeof t.then == "function") return f(e, po(t), n);
				if (t.$$typeof === se) return f(e, ja(e, t), n);
				ho(e, t);
			}
			return null;
		}
		function p(e, t, n, r) {
			var i = t === null ? null : t.key;
			if (typeof n == "string" && n !== "" || typeof n == "number" || typeof n == "bigint") return i === null ? c(e, t, "" + n, r) : null;
			if (typeof n == "object" && n) {
				switch (n.$$typeof) {
					case ne: return n.key === i ? l(e, t, n, r) : null;
					case re: return n.key === i ? u(e, t, n, r) : null;
					case fe: return n = oo(n), p(e, t, n, r);
				}
				if (xe(n) || ve(n)) return i === null ? d(e, t, n, r, null) : null;
				if (typeof n.then == "function") return p(e, t, po(n), r);
				if (n.$$typeof === se) return p(e, t, ja(e, n), r);
				ho(e, n);
			}
			return null;
		}
		function m(e, t, n, r, i) {
			if (typeof r == "string" && r !== "" || typeof r == "number" || typeof r == "bigint") return e = e.get(n) || null, c(t, e, "" + r, i);
			if (typeof r == "object" && r) {
				switch (r.$$typeof) {
					case ne: return e = e.get(r.key === null ? n : r.key) || null, l(t, e, r, i);
					case re: return e = e.get(r.key === null ? n : r.key) || null, u(t, e, r, i);
					case fe: return r = oo(r), m(e, t, n, r, i);
				}
				if (xe(r) || ve(r)) return e = e.get(n) || null, d(t, e, r, i, null);
				if (typeof r.then == "function") return m(e, t, n, po(r), i);
				if (r.$$typeof === se) return m(e, t, n, ja(t, r), i);
				ho(t, r);
			}
			return null;
		}
		function h(i, a, s, c) {
			for (var l = null, u = null, d = a, h = a = 0, g = null; d !== null && h < s.length; h++) {
				d.index > h ? (g = d, d = null) : g = d.sibling;
				var _ = p(i, d, s[h], c);
				if (_ === null) {
					d === null && (d = g);
					break;
				}
				e && d && _.alternate === null && t(i, d), a = o(_, a, h), u === null ? l = _ : u.sibling = _, u = _, d = g;
			}
			if (h === s.length) return n(i, d), F && ia(i, h), l;
			if (d === null) {
				for (; h < s.length; h++) d = f(i, s[h], c), d !== null && (a = o(d, a, h), u === null ? l = d : u.sibling = d, u = d);
				return F && ia(i, h), l;
			}
			for (d = r(d); h < s.length; h++) g = m(d, i, h, s[h], c), g !== null && (e && (_ = g.alternate, _ !== null && d.delete(_.key === null ? h : _.key)), a = o(g, a, h), u === null ? l = g : u.sibling = g, u = g);
			return e && d.forEach(function(e) {
				return t(i, e);
			}), F && ia(i, h), l;
		}
		function g(a, s, c, l) {
			if (c == null) throw Error(i(151));
			for (var u = null, d = null, h = s, g = s = 0, _ = null, v = c.next(); h !== null && !v.done; g++, v = c.next()) {
				h.index > g ? (_ = h, h = null) : _ = h.sibling;
				var y = p(a, h, v.value, l);
				if (y === null) {
					h === null && (h = _);
					break;
				}
				e && h && y.alternate === null && t(a, h), s = o(y, s, g), d === null ? u = y : d.sibling = y, d = y, h = _;
			}
			if (v.done) return n(a, h), F && ia(a, g), u;
			if (h === null) {
				for (; !v.done; g++, v = c.next()) v = f(a, v.value, l), v !== null && (s = o(v, s, g), d === null ? u = v : d.sibling = v, d = v);
				return F && ia(a, g), u;
			}
			for (h = r(h); !v.done; g++, v = c.next()) v = m(h, a, g, v.value, l), v !== null && (e && (_ = v.alternate, _ !== null && h.delete(_.key === null ? g : _.key)), s = o(v, s, g), d === null ? u = v : d.sibling = v, d = v);
			return e && h.forEach(function(e) {
				return t(a, e);
			}), F && ia(a, g), u;
		}
		function _(e, r, o, c) {
			if (typeof o == "object" && o && o.type === D && o.key === null && o.props.ref === void 0 && (o = o.props.children), typeof o == "object" && o) {
				switch (o.$$typeof) {
					case ne:
						a: {
							for (var l = o.key; r !== null;) {
								if (r.key === l) {
									if (l = o.type, l === D) {
										if (r.tag === 7) {
											n(e, r.sibling), c = a(r, o.props.children), mo(c, o), c.return = e, e = c;
											break a;
										}
									} else if (r.elementType === l || typeof l == "object" && l && l.$$typeof === fe && oo(l) === r.type) {
										n(e, r.sibling), c = a(r, o.props), mo(c, o), c.return = e, e = c;
										break a;
									}
									n(e, r);
									break;
								}
								t(e, r), r = r.sibling;
							}
							o.type === D ? (c = Ui(o.props.children, e.mode, c, o.key), mo(c, o), c.return = e, e = c) : (c = Hi(o.type, o.key, o.props, null, e.mode, c), mo(c, o), c.return = e, e = c);
						}
						return s(e);
					case re:
						a: {
							for (l = o.key; r !== null;) {
								if (r.key === l) {
									if (r.tag === 4 && r.stateNode.containerInfo === o.containerInfo && r.stateNode.implementation === o.implementation) {
										n(e, r.sibling), c = a(r, o.children || []), c.return = e, e = c;
										break a;
									}
									n(e, r);
									break;
								}
								t(e, r), r = r.sibling;
							}
							c = Ki(o, e.mode, c), c.return = e, e = c;
						}
						return s(e);
					case fe: return o = oo(o), _(e, r, o, c);
				}
				if (xe(o)) return h(e, r, o, c);
				if (ve(o)) {
					if (l = ve(o), typeof l != "function") throw Error(i(150));
					return o = l.call(o), g(e, r, o, c);
				}
				if (typeof o.then == "function") return _(e, r, po(o), c);
				if (o.$$typeof === se) return _(e, r, ja(e, o), c);
				ho(e, o);
			}
			return typeof o == "string" && o !== "" || typeof o == "number" || typeof o == "bigint" ? (o = "" + o, r !== null && r.tag === 6 ? (n(e, r.sibling), c = a(r, o), c.return = e, e = c) : (n(e, r), c = Wi(o, e.mode, c), c.return = e, e = c), s(e)) : n(e, r);
		}
		return function(e, t, n, r) {
			try {
				fo = 0;
				var i = _(e, t, n, r);
				return uo = null, i;
			} catch (t) {
				if (t === eo || t === no) throw t;
				var a = Ri(29, t, null, e.mode);
				return a.lanes = r, a.return = e, a;
			}
		};
	}
	var _o = go(!0), vo = go(!1), yo = !1;
	function bo(e) {
		e.updateQueue = {
			baseState: e.memoizedState,
			firstBaseUpdate: null,
			lastBaseUpdate: null,
			shared: {
				pending: null,
				lanes: 0,
				hiddenCallbacks: null
			},
			callbacks: null
		};
	}
	function xo(e, t) {
		e = e.updateQueue, t.updateQueue === e && (t.updateQueue = {
			baseState: e.baseState,
			firstBaseUpdate: e.firstBaseUpdate,
			lastBaseUpdate: e.lastBaseUpdate,
			shared: e.shared,
			callbacks: null
		});
	}
	function So(e) {
		return {
			lane: e,
			tag: 0,
			payload: null,
			callback: null,
			next: null
		};
	}
	function Co(e, t, n) {
		var r = e.updateQueue;
		if (r === null) return null;
		if (r = r.shared, W & 2) {
			var i = r.pending;
			return i === null ? t.next = t : (t.next = i.next, i.next = t), r.pending = t, t = Fi(e), Pi(e, null, n), t;
		}
		return ji(e, r, t, n), Fi(e);
	}
	function wo(e, t, n) {
		if (t = t.updateQueue, t !== null && (t = t.shared, n & 4194048)) {
			var r = t.lanes;
			r &= e.pendingLanes, n |= r, t.lanes = n, xt(e, n);
		}
	}
	function To(e, t) {
		var n = e.updateQueue, r = e.alternate;
		if (r !== null && (r = r.updateQueue, n === r)) {
			var i = null, a = null;
			if (n = n.firstBaseUpdate, n !== null) {
				do {
					var o = {
						lane: n.lane,
						tag: n.tag,
						payload: n.payload,
						callback: null,
						next: null
					};
					a === null ? i = a = o : a = a.next = o, n = n.next;
				} while (n !== null);
				a === null ? i = a = t : a = a.next = t;
			} else i = a = t;
			n = {
				baseState: r.baseState,
				firstBaseUpdate: i,
				lastBaseUpdate: a,
				shared: r.shared,
				callbacks: r.callbacks
			}, e.updateQueue = n;
		} else e = n.lastBaseUpdate, e === null ? n.firstBaseUpdate = t : e.next = t, n.lastBaseUpdate = t;
	}
	var Eo = !1;
	function Do() {
		if (Eo) {
			var e = Ga;
			if (e !== null) throw e;
		}
	}
	function Oo(e, t, n, r) {
		Eo = !1;
		var i = e.updateQueue;
		yo = !1;
		var a = i.firstBaseUpdate, o = i.lastBaseUpdate, s = i.shared.pending;
		if (s !== null) {
			i.shared.pending = null;
			var c = s, l = c.next;
			c.next = null, o === null ? a = l : o.next = l, o = c;
			var u = e.alternate;
			u !== null && (u = u.updateQueue, s = u.lastBaseUpdate, s !== o && (s === null ? u.firstBaseUpdate = l : s.next = l, u.lastBaseUpdate = c));
		}
		if (a !== null) {
			var d = i.baseState;
			o = 0, u = l = c = null, s = a;
			do {
				var f = s.lane & -536870913, p = f !== s.lane;
				if (p ? (q & f) === f : (r & f) === f) {
					f !== 0 && f === Wa && (Eo = !0), u !== null && (u = u.next = {
						lane: 0,
						tag: s.tag,
						payload: s.payload,
						callback: null,
						next: null
					});
					a: {
						var m = e, h = s;
						f = t;
						var g = n;
						switch (h.tag) {
							case 1:
								if (m = h.payload, typeof m == "function") {
									d = m.call(g, d, f);
									break a;
								}
								d = m;
								break a;
							case 3: m.flags = m.flags & -65537 | 128;
							case 0:
								if (m = h.payload, f = typeof m == "function" ? m.call(g, d, f) : m, f == null) break a;
								d = E({}, d, f);
								break a;
							case 2: yo = !0;
						}
					}
					f = s.callback, f !== null && (e.flags |= 64, p && (e.flags |= 8192), p = i.callbacks, p === null ? i.callbacks = [f] : p.push(f));
				} else p = {
					lane: f,
					tag: s.tag,
					payload: s.payload,
					callback: s.callback,
					next: null
				}, u === null ? (l = u = p, c = d) : u = u.next = p, o |= f;
				if (s = s.next, s === null) {
					if (s = i.shared.pending, s === null) break;
					p = s, s = p.next, p.next = null, i.lastBaseUpdate = p, i.shared.pending = null;
				}
			} while (1);
			u === null && (c = d), i.baseState = c, i.firstBaseUpdate = l, i.lastBaseUpdate = u, a === null && (i.shared.lanes = 0), sd |= o, e.lanes = o, e.memoizedState = d;
		}
	}
	function ko(e, t) {
		if (typeof e != "function") throw Error(i(191, e));
		e.call(t);
	}
	function Ao(e, t) {
		var n = e.callbacks;
		if (n !== null) for (e.callbacks = null, e = 0; e < n.length; e++) ko(n[e], t);
	}
	var jo = Te(null), Mo = Te(0);
	function No(e, t) {
		e = od, j(Mo, e), j(jo, t), od = e | t.baseLanes;
	}
	function Po() {
		j(Mo, od), j(jo, jo.current);
	}
	function Fo() {
		od = Mo.current, Ee(jo), Ee(Mo);
	}
	var Io = Te(null), Lo = null;
	function Ro(e) {
		var t = e.alternate;
		j(Uo, Uo.current & 1), j(Io, e), Lo === null && (t === null || jo.current !== null || t.memoizedState !== null) && (Lo = e);
	}
	function zo(e) {
		j(Uo, Uo.current), j(Io, e), Lo === null && (Lo = e);
	}
	function Bo(e) {
		e.tag === 22 ? (j(Uo, Uo.current), j(Io, e), Lo === null && (Lo = e)) : Vo();
	}
	function Vo() {
		j(Uo, Uo.current), j(Io, Io.current);
	}
	function Ho(e) {
		Ee(Io), Lo === e && (Lo = null), Ee(Uo);
	}
	var Uo = Te(0);
	function Wo(e, t) {
		j(Io, Io.current), j(Uo, t);
	}
	function Go(e) {
		Ee(Uo), Ee(Io), Lo === e && (Lo = null);
	}
	function Ko(e) {
		for (var t = e; t !== null;) {
			if (t.tag === 13) {
				var n = t.memoizedState;
				if (n !== null && (n = n.dehydrated, n === null || om(n) || sm(n))) return t;
			} else if (t.tag === 19 && t.memoizedProps.revealOrder !== "independent") {
				if (t.flags & 128) return t;
			} else if (t.child !== null) {
				t.child.return = t, t = t.child;
				continue;
			}
			if (t === e) break;
			for (; t.sibling === null;) {
				if (t.return === null || t.return === e) return null;
				t = t.return;
			}
			t.sibling.return = t.return, t = t.sibling;
		}
		return null;
	}
	var qo = 0, I = null, L = null, R = null, Jo = !1, Yo = !1, Xo = !1, Zo = 0, Qo = 0, $o = null, es = 0;
	function z() {
		throw Error(i(321));
	}
	function ts(e, t) {
		if (t === null) return !1;
		for (var n = 0; n < t.length && n < e.length; n++) if (!qr(e[n], t[n])) return !1;
		return !0;
	}
	function ns(e, t, n, r, i, a) {
		return qo = a, I = t, t.memoizedState = null, t.updateQueue = null, t.lanes = 0, k.H = e === null || e.memoizedState === null ? vc : yc, Xo = !1, a = n(r, i), Xo = !1, Yo && (a = is(t, n, r, i)), rs(e), a;
	}
	function rs(e) {
		k.H = _c;
		var t = L !== null && L.next !== null;
		if (qo = 0, R = L = I = null, Jo = !1, Qo = 0, $o = null, t) throw Error(i(300));
		e === null || Ic || (e = e.dependencies, e !== null && Oa(e) && (Ic = !0));
	}
	function is(e, t, n, r) {
		I = e;
		var a = 0;
		do {
			if (Yo && ($o = null), Qo = 0, Yo = !1, 25 <= a) throw Error(i(301));
			if (a += 1, R = L = null, e.updateQueue != null) {
				var o = e.updateQueue;
				o.lastEffect = null, o.events = null, o.stores = null, o.memoCache != null && (o.memoCache.index = 0);
			}
			k.H = bc, o = t(n, r);
		} while (Yo);
		return o;
	}
	function as() {
		var e = k.H, t = e.useState()[0];
		return t = typeof t.then == "function" ? ds(t) : t, e = e.useState()[0], (L === null ? null : L.memoizedState) !== e && (I.flags |= 1024), t;
	}
	function os() {
		var e = Zo !== 0;
		return Zo = 0, e;
	}
	function ss(e, t, n) {
		t.updateQueue = e.updateQueue, t.flags &= -2053, e.lanes &= ~n;
	}
	function cs(e) {
		if (Jo) {
			for (e = e.memoizedState; e !== null;) {
				var t = e.queue;
				t !== null && (t.pending = null), e = e.next;
			}
			Jo = !1;
		}
		qo = 0, R = L = I = null, Yo = !1, Qo = Zo = 0, $o = null;
	}
	function ls() {
		var e = {
			memoizedState: null,
			baseState: null,
			baseQueue: null,
			queue: null,
			next: null
		};
		return R === null ? I.memoizedState = R = e : R = R.next = e, R;
	}
	function B() {
		if (L === null) {
			var e = I.alternate;
			e = e === null ? null : e.memoizedState;
		} else e = L.next;
		var t = R === null ? I.memoizedState : R.next;
		if (t !== null) R = t, L = e;
		else {
			if (e === null) throw I.alternate === null ? Error(i(467)) : Error(i(310));
			L = e, e = {
				memoizedState: L.memoizedState,
				baseState: L.baseState,
				baseQueue: L.baseQueue,
				queue: L.queue,
				next: null
			}, R === null ? I.memoizedState = R = e : R = R.next = e;
		}
		return R;
	}
	function us() {
		return {
			lastEffect: null,
			events: null,
			stores: null,
			memoCache: null
		};
	}
	function ds(e) {
		var t = Qo;
		return Qo += 1, $o === null && ($o = []), e = ao($o, e, t), t = I, (R === null ? t.memoizedState : R.next) === null && (t = t.alternate, k.H = t === null || t.memoizedState === null ? vc : yc), e;
	}
	function fs(e) {
		if (typeof e == "object" && e) {
			if (typeof e.then == "function") return ds(e);
			if (e.$$typeof === ge) return;
			if (e.$$typeof === se) return Aa(e);
		}
		throw Error(i(438, String(e)));
	}
	function ps(e) {
		var t = null, n = I.updateQueue;
		if (n !== null && (t = n.memoCache), t == null) {
			var r = I.alternate;
			r !== null && (r = r.updateQueue, r !== null && (r = r.memoCache, r != null && (t = {
				data: r.data.map(function(e) {
					return e.slice();
				}),
				index: 0
			})));
		}
		if (t ??= {
			data: [],
			index: 0
		}, n === null && (n = us(), I.updateQueue = n), n.memoCache = t, n = t.data[t.index], n === void 0) for (n = t.data[t.index] = Array(e), r = 0; r < e; r++) n[r] = he;
		return t.index++, n;
	}
	function ms(e, t) {
		return typeof t == "function" ? t(e) : t;
	}
	function hs(e) {
		return gs(B(), L, e);
	}
	function gs(e, t, n) {
		var r = e.queue;
		if (r === null) throw Error(i(311));
		r.lastRenderedReducer = n;
		var a = e.baseQueue, o = r.pending;
		if (o !== null) {
			if (a !== null) {
				var s = a.next;
				a.next = o.next, o.next = s;
			}
			t.baseQueue = a = o, r.pending = null;
		}
		if (o = e.baseState, a === null) e.memoizedState = o;
		else {
			t = a.next;
			var c = s = null, l = null, u = t, d = !1;
			do {
				var f = u.lane & -536870913;
				if (f === u.lane ? (qo & f) === f : (q & f) === f) {
					var p = u.revertLane;
					if (p === 0) l !== null && (l = l.next = {
						lane: 0,
						revertLane: 0,
						gesture: null,
						action: u.action,
						hasEagerState: u.hasEagerState,
						eagerState: u.eagerState,
						next: null
					}), f === Wa && (d = !0);
					else if ((qo & p) === p) {
						u = u.next, p === Wa && (d = !0);
						continue;
					} else f = {
						lane: 0,
						revertLane: u.revertLane,
						gesture: null,
						action: u.action,
						hasEagerState: u.hasEagerState,
						eagerState: u.eagerState,
						next: null
					}, l === null ? (c = l = f, s = o) : l = l.next = f, I.lanes |= p, sd |= p;
					f = u.action, Xo && n(o, f), o = u.hasEagerState ? u.eagerState : n(o, f);
				} else p = {
					lane: f,
					revertLane: u.revertLane,
					gesture: u.gesture,
					action: u.action,
					hasEagerState: u.hasEagerState,
					eagerState: u.eagerState,
					next: null
				}, l === null ? (c = l = p, s = o) : l = l.next = p, I.lanes |= f, sd |= f;
				u = u.next;
			} while (u !== null && u !== t);
			if (l === null ? s = o : l.next = c, !qr(o, e.memoizedState) && (Ic = !0, d && (n = Ga, n !== null))) throw n;
			e.memoizedState = o, e.baseState = s, e.baseQueue = l, r.lastRenderedState = o;
		}
		return a === null && (r.lanes = 0), [e.memoizedState, r.dispatch];
	}
	function _s(e) {
		var t = B(), n = t.queue;
		if (n === null) throw Error(i(311));
		n.lastRenderedReducer = e;
		var r = n.dispatch, a = n.pending, o = t.memoizedState;
		if (a !== null) {
			n.pending = null;
			var s = a = a.next;
			do
				o = e(o, s.action), s = s.next;
			while (s !== a);
			qr(o, t.memoizedState) || (Ic = !0), t.memoizedState = o, t.baseQueue === null && (t.baseState = o), n.lastRenderedState = o;
		}
		return [o, r];
	}
	function vs(e, t, n) {
		var r = I, a = B(), o = F;
		if (o) {
			if (n === void 0) throw Error(i(407));
			n = n();
		} else n = t();
		var s = !qr((L || a).memoizedState, n);
		if (s && (a.memoizedState = n, Ic = !0), a = a.queue, Us(xs.bind(null, r, a, e), [e]), e = a.getSnapshot !== t || s || R !== null && !!(R.memoizedState.tag & 1), Rs(e ? 9 : 8, { destroy: void 0 }, bs.bind(null, r, a, n, t), null), e) {
			if (r.flags |= 2048, G === null) throw Error(i(349));
			o || qo & 127 || ys(r, t, n);
		}
		return n;
	}
	function ys(e, t, n) {
		e.flags |= 16384, e = {
			getSnapshot: t,
			value: n
		}, t = I.updateQueue, t === null ? (t = us(), I.updateQueue = t, t.stores = [e]) : (n = t.stores, n === null ? t.stores = [e] : n.push(e));
	}
	function bs(e, t, n, r) {
		t.value = n, t.getSnapshot = r, Ss(t) && Cs(e);
	}
	function xs(e, t, n) {
		return n(function() {
			Ss(t) && Cs(e);
		});
	}
	function Ss(e) {
		var t = e.getSnapshot;
		e = e.value;
		try {
			var n = t();
			return !qr(e, n);
		} catch {
			return !0;
		}
	}
	function Cs(e) {
		var t = Ni(e, 2);
		t !== null && Pd(t, e, 2);
	}
	function ws(e) {
		var t = ls();
		if (typeof e == "function") {
			var n = e;
			if (e = n(), Xo) {
				it(!0);
				try {
					n();
				} finally {
					it(!1);
				}
			}
		}
		return t.memoizedState = t.baseState = e, t.queue = {
			pending: null,
			lanes: 0,
			dispatch: null,
			lastRenderedReducer: ms,
			lastRenderedState: e
		}, t;
	}
	function Ts(e, t, n, r) {
		return e.baseState = n, gs(e, L, typeof r == "function" ? r : ms);
	}
	function Es(e, t, n, r, a) {
		if (mc(e)) throw Error(i(485));
		if (e = t.action, e !== null) {
			var o = {
				payload: a,
				action: e,
				next: null,
				isTransition: !0,
				status: "pending",
				value: null,
				reason: null,
				listeners: [],
				then: function(e) {
					o.listeners.push(e);
				}
			};
			k.T === null ? o.isTransition = !1 : n(!0), r(o), n = t.pending, n === null ? (o.next = t.pending = o, Ds(t, o)) : (o.next = n.next, t.pending = n.next = o);
		}
	}
	function Ds(e, t) {
		var n = t.action, r = t.payload, i = e.state;
		if (t.isTransition) {
			var a = k.T, o = {};
			o.types = a === null ? null : a.types, k.T = o;
			try {
				var s = n(i, r), c = k.S;
				c !== null && c(o, s), Os(e, t, s);
			} catch (n) {
				As(e, t, n);
			} finally {
				a !== null && o.types !== null && (a.types = o.types), k.T = a;
			}
		} else try {
			a = n(i, r), Os(e, t, a);
		} catch (n) {
			As(e, t, n);
		}
	}
	function Os(e, t, n) {
		typeof n == "object" && n && typeof n.then == "function" ? n.then(function(n) {
			ks(e, t, n);
		}, function(n) {
			return As(e, t, n);
		}) : ks(e, t, n);
	}
	function ks(e, t, n) {
		t.status = "fulfilled", t.value = n, js(t), e.state = n, t = e.pending, t !== null && (n = t.next, n === t ? e.pending = null : (n = n.next, t.next = n, Ds(e, n)));
	}
	function As(e, t, n) {
		var r = e.pending;
		if (e.pending = null, r !== null) {
			r = r.next;
			do
				t.status = "rejected", t.reason = n, js(t), t = t.next;
			while (t !== r);
		}
		e.action = null;
	}
	function js(e) {
		e = e.listeners;
		for (var t = 0; t < e.length; t++) (0, e[t])();
	}
	function Ms(e, t) {
		return t;
	}
	function Ns(e, t) {
		if (F) {
			var n = G.formState;
			if (n !== null) {
				a: {
					var r = I;
					if (F) {
						if (P) {
							b: {
								for (var i = P, a = da; i.nodeType !== 8;) if (!a || (i = lm(i.nextSibling), i === null)) {
									i = null;
									break b;
								}
								a = i.data, i = a === "F!" || a === "F" ? i : null;
							}
							if (i) {
								P = lm(i.nextSibling), r = i.data === "F!";
								break a;
							}
						}
						pa(r);
					}
					r = !1;
				}
				r && (t = n[0]);
			}
		}
		return n = ls(), n.memoizedState = n.baseState = t, r = {
			pending: null,
			lanes: 0,
			dispatch: null,
			lastRenderedReducer: Ms,
			lastRenderedState: t
		}, n.queue = r, n = dc.bind(null, I, r), r.dispatch = n, r = ws(!1), a = pc.bind(null, I, !1, r.queue), r = ls(), i = {
			state: t,
			dispatch: null,
			action: e,
			pending: null
		}, r.queue = i, n = Es.bind(null, I, i, a, n), i.dispatch = n, r.memoizedState = e, [
			t,
			n,
			!1
		];
	}
	function Ps(e) {
		return Fs(B(), L, e);
	}
	function Fs(e, t, n) {
		if (t = gs(e, t, Ms)[0], e = hs(ms)[0], typeof t == "object" && t && typeof t.then == "function") try {
			var r = ds(t);
		} catch (e) {
			throw e === eo ? no : e;
		}
		else r = t;
		t = B();
		var i = t.queue, a = i.dispatch;
		return n !== t.memoizedState && (I.flags |= 2048, Rs(9, { destroy: void 0 }, Is.bind(null, i, n), null)), [
			r,
			a,
			e
		];
	}
	function Is(e, t) {
		e.action = t;
	}
	function Ls(e) {
		var t = B(), n = L;
		if (n !== null) return Fs(t, n, e);
		B(), t = t.memoizedState, n = B();
		var r = n.queue.dispatch;
		return n.memoizedState = e, [
			t,
			r,
			!1
		];
	}
	function Rs(e, t, n, r) {
		return e = {
			tag: e,
			create: n,
			deps: r,
			inst: t,
			next: null
		}, t = I.updateQueue, t === null && (t = us(), I.updateQueue = t), n = t.lastEffect, n === null ? t.lastEffect = e.next = e : (r = n.next, n.next = e, e.next = r, t.lastEffect = e), e;
	}
	function zs() {
		return B().memoizedState;
	}
	function Bs(e, t, n, r) {
		var i = ls();
		I.flags |= e, i.memoizedState = Rs(1 | t, { destroy: void 0 }, n, r === void 0 ? null : r);
	}
	function Vs(e, t, n, r) {
		var i = B();
		r = r === void 0 ? null : r;
		var a = i.memoizedState.inst;
		L !== null && r !== null && ts(r, L.memoizedState.deps) ? i.memoizedState = Rs(t, a, n, r) : (I.flags |= e, i.memoizedState = Rs(1 | t, a, n, r));
	}
	function Hs(e, t) {
		Bs(8390656, 8, e, t);
	}
	function Us(e, t) {
		Vs(2048, 8, e, t);
	}
	function Ws(e) {
		I.flags |= 4;
		var t = I.updateQueue;
		if (t === null) t = us(), I.updateQueue = t, t.events = [e];
		else {
			var n = t.events;
			n === null ? t.events = [e] : n.push(e);
		}
	}
	function Gs(e) {
		var t = B().memoizedState;
		return Ws({
			ref: t,
			nextImpl: e
		}), function() {
			if (W & 2) throw Error(i(440));
			return t.impl.apply(void 0, arguments);
		};
	}
	function Ks(e, t) {
		return Vs(4, 2, e, t);
	}
	function qs(e, t) {
		return Vs(4, 4, e, t);
	}
	function Js(e, t) {
		if (typeof t == "function") {
			e = e();
			var n = t(e);
			return function() {
				typeof n == "function" ? n() : t(null);
			};
		}
		if (t != null) return e = e(), t.current = e, function() {
			t.current = null;
		};
	}
	function Ys(e, t, n) {
		n = n == null ? null : n.concat([e]), Vs(4, 4, Js.bind(null, t, e), n);
	}
	function Xs() {}
	function Zs(e, t) {
		var n = B();
		t = t === void 0 ? null : t;
		var r = n.memoizedState;
		return t !== null && ts(t, r[1]) ? r[0] : (n.memoizedState = [e, t], e);
	}
	function Qs(e, t) {
		var n = B();
		t = t === void 0 ? null : t;
		var r = n.memoizedState;
		if (t !== null && ts(t, r[1])) return r[0];
		if (r = e(), Xo) {
			it(!0);
			try {
				e();
			} finally {
				it(!1);
			}
		}
		return n.memoizedState = [r, t], r;
	}
	function $s(e, t, n) {
		return n === void 0 || qo & 1073741824 && !(q & 261930) ? e.memoizedState = t : (e.memoizedState = n, e = Md(), I.lanes |= e, sd |= e, n);
	}
	function ec(e, t, n, r) {
		return qr(n, t) ? n : jo.current === null ? !(qo & 106) || qo & 1073741824 && !(q & 261930) ? (Ic = !0, e.memoizedState = n) : (e = Md(), I.lanes |= e, sd |= e, t) : (e = $s(e, n, r), qr(e, t) || (Ic = !0), e);
	}
	function tc(e, t, n, r, i) {
		var a = A.p;
		A.p = a !== 0 && 8 > a ? a : 8;
		var o = k.T, s = {};
		s.types = o === null ? null : o.types, k.T = s, pc(e, !1, t, n);
		try {
			var c = i(), l = k.S;
			l !== null && l(s, c), typeof c == "object" && c && typeof c.then == "function" ? fc(e, t, Ja(c, r), jd(e)) : fc(e, t, r, jd(e));
		} catch (n) {
			fc(e, t, {
				then: function() {},
				status: "rejected",
				reason: n
			}, jd());
		} finally {
			A.p = a, o !== null && s.types !== null && (o.types = s.types), k.T = o;
		}
	}
	function nc() {}
	function rc(e, t, n, r) {
		if (e.tag !== 5) throw Error(i(476));
		var a = ic(e).queue;
		tc(e, a, t, Se, n === null ? nc : function() {
			return ac(e), n(r);
		});
	}
	function ic(e) {
		var t = e.memoizedState;
		if (t !== null) return t;
		t = {
			memoizedState: Se,
			baseState: Se,
			baseQueue: null,
			queue: {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: ms,
				lastRenderedState: Se
			},
			next: null
		};
		var n = {};
		return t.next = {
			memoizedState: n,
			baseState: n,
			baseQueue: null,
			queue: {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: ms,
				lastRenderedState: n
			},
			next: null
		}, e.memoizedState = t, e = e.alternate, e !== null && (e.memoizedState = t), t;
	}
	function ac(e) {
		var t = ic(e);
		t.next === null && (t = e.alternate.memoizedState), fc(e, t.next.queue, {}, jd());
	}
	function oc() {
		return Aa(sh);
	}
	function sc() {
		return B().memoizedState;
	}
	function cc() {
		return B().memoizedState;
	}
	function lc(e) {
		for (var t = e.return; t !== null;) {
			switch (t.tag) {
				case 24:
				case 3:
					var n = jd();
					e = So(n);
					var r = Co(t, e, n);
					r !== null && (Pd(r, t, n), wo(r, t, n)), t = { cache: La() }, e.payload = t;
					return;
			}
			t = t.return;
		}
	}
	function uc(e, t, n) {
		var r = jd();
		n = {
			lane: r,
			revertLane: 0,
			gesture: null,
			action: n,
			hasEagerState: !1,
			eagerState: null,
			next: null
		}, mc(e) ? hc(t, n) : (n = Mi(e, t, n, r), n !== null && (Pd(n, e, r), gc(n, t, r)));
	}
	function dc(e, t, n) {
		fc(e, t, n, jd());
	}
	function fc(e, t, n, r) {
		var i = {
			lane: r,
			revertLane: 0,
			gesture: null,
			action: n,
			hasEagerState: !1,
			eagerState: null,
			next: null
		};
		if (mc(e)) hc(t, i);
		else {
			var a = e.alternate;
			if (e.lanes === 0 && (a === null || a.lanes === 0) && (a = t.lastRenderedReducer, a !== null)) try {
				var o = t.lastRenderedState, s = a(o, n);
				if (i.hasEagerState = !0, i.eagerState = s, qr(s, o)) return ji(e, t, i, 0), G === null && Ai(), !1;
			} catch {}
			if (n = Mi(e, t, i, r), n !== null) return Pd(n, e, r), gc(n, t, r), !0;
		}
		return !1;
	}
	function pc(e, t, n, r) {
		if (r = {
			lane: 2,
			revertLane: Pf(),
			gesture: null,
			action: r,
			hasEagerState: !1,
			eagerState: null,
			next: null
		}, mc(e)) {
			if (t) throw Error(i(479));
		} else t = Mi(e, n, r, 2), t !== null && Pd(t, e, 2);
	}
	function mc(e) {
		var t = e.alternate;
		return e === I || t !== null && t === I;
	}
	function hc(e, t) {
		Yo = Jo = !0;
		var n = e.pending;
		n === null ? t.next = t : (t.next = n.next, n.next = t), e.pending = t;
	}
	function gc(e, t, n) {
		if (n & 4194048) {
			var r = t.lanes;
			r &= e.pendingLanes, n |= r, t.lanes = n, xt(e, n);
		}
	}
	var _c = {
		readContext: Aa,
		use: fs,
		useCallback: z,
		useContext: z,
		useEffect: z,
		useImperativeHandle: z,
		useLayoutEffect: z,
		useInsertionEffect: z,
		useMemo: z,
		useReducer: z,
		useRef: z,
		useState: z,
		useDebugValue: z,
		useDeferredValue: z,
		useTransition: z,
		useSyncExternalStore: z,
		useId: z,
		useHostTransitionStatus: z,
		useFormState: z,
		useActionState: z,
		useOptimistic: z,
		useMemoCache: z,
		useCacheRefresh: z,
		useEffectEvent: z
	}, vc = {
		readContext: Aa,
		use: fs,
		useCallback: function(e, t) {
			return ls().memoizedState = [e, t === void 0 ? null : t], e;
		},
		useContext: Aa,
		useEffect: Hs,
		useImperativeHandle: function(e, t, n) {
			n = n == null ? null : n.concat([e]), Bs(4194308, 4, Js.bind(null, t, e), n);
		},
		useLayoutEffect: function(e, t) {
			return Bs(4194308, 4, e, t);
		},
		useInsertionEffect: function(e, t) {
			Bs(4, 2, e, t);
		},
		useMemo: function(e, t) {
			var n = ls();
			t = t === void 0 ? null : t;
			var r = e();
			if (Xo) {
				it(!0);
				try {
					e();
				} finally {
					it(!1);
				}
			}
			return n.memoizedState = [r, t], r;
		},
		useReducer: function(e, t, n) {
			var r = ls();
			if (n !== void 0) {
				var i = n(t);
				if (Xo) {
					it(!0);
					try {
						n(t);
					} finally {
						it(!1);
					}
				}
			} else i = t;
			return r.memoizedState = r.baseState = i, e = {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: e,
				lastRenderedState: i
			}, r.queue = e, e = e.dispatch = uc.bind(null, I, e), [r.memoizedState, e];
		},
		useRef: function(e) {
			var t = ls();
			return e = { current: e }, t.memoizedState = e;
		},
		useState: function(e) {
			e = ws(e);
			var t = e.queue, n = dc.bind(null, I, t);
			return t.dispatch = n, [e.memoizedState, n];
		},
		useDebugValue: Xs,
		useDeferredValue: function(e, t) {
			return $s(ls(), e, t);
		},
		useTransition: function() {
			var e = ws(!1);
			return e = tc.bind(null, I, e.queue, !0, !1), ls().memoizedState = e, [!1, e];
		},
		useSyncExternalStore: function(e, t, n) {
			var r = I, a = ls();
			if (F) {
				if (n === void 0) throw Error(i(407));
				n = n();
			} else {
				if (n = t(), G === null) throw Error(i(349));
				q & 127 || ys(r, t, n);
			}
			a.memoizedState = n;
			var o = {
				value: n,
				getSnapshot: t
			};
			return a.queue = o, Hs(xs.bind(null, r, o, e), [e]), r.flags |= 2048, Rs(9, { destroy: void 0 }, bs.bind(null, r, o, n, t), null), n;
		},
		useId: function() {
			var e = ls(), t = G.identifierPrefix;
			if (F) {
				var n = ra, r = na;
				n = (r & ~(1 << 32 - at(r) - 1)).toString(32) + n, t = "_" + t + "R_" + n, n = Zo++, 0 < n && (t += "H" + n.toString(32)), t += "_";
			} else n = es++, t = "_" + t + "r_" + n.toString(32) + "_";
			return e.memoizedState = t;
		},
		useHostTransitionStatus: oc,
		useFormState: Ns,
		useActionState: Ns,
		useOptimistic: function(e) {
			var t = ls();
			t.memoizedState = t.baseState = e;
			var n = {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: null,
				lastRenderedState: null
			};
			return t.queue = n, t = pc.bind(null, I, !0, n), n.dispatch = t, [e, t];
		},
		useMemoCache: ps,
		useCacheRefresh: function() {
			return ls().memoizedState = lc.bind(null, I);
		},
		useEffectEvent: function(e) {
			var t = ls(), n = { impl: e };
			return t.memoizedState = n, function() {
				if (W & 2) throw Error(i(440));
				return n.impl.apply(void 0, arguments);
			};
		}
	}, yc = {
		readContext: Aa,
		use: fs,
		useCallback: Zs,
		useContext: Aa,
		useEffect: Us,
		useImperativeHandle: Ys,
		useInsertionEffect: Ks,
		useLayoutEffect: qs,
		useMemo: Qs,
		useReducer: hs,
		useRef: zs,
		useState: function() {
			return hs(ms);
		},
		useDebugValue: Xs,
		useDeferredValue: function(e, t) {
			return ec(B(), L.memoizedState, e, t);
		},
		useTransition: function() {
			var e = hs(ms)[0], t = B().memoizedState;
			return [typeof e == "boolean" ? e : ds(e), t];
		},
		useSyncExternalStore: vs,
		useId: sc,
		useHostTransitionStatus: oc,
		useFormState: Ps,
		useActionState: Ps,
		useOptimistic: function(e, t) {
			return Ts(B(), L, e, t);
		},
		useMemoCache: ps,
		useCacheRefresh: cc,
		useEffectEvent: Gs
	}, bc = {
		readContext: Aa,
		use: fs,
		useCallback: Zs,
		useContext: Aa,
		useEffect: Us,
		useImperativeHandle: Ys,
		useInsertionEffect: Ks,
		useLayoutEffect: qs,
		useMemo: Qs,
		useReducer: _s,
		useRef: zs,
		useState: function() {
			return _s(ms);
		},
		useDebugValue: Xs,
		useDeferredValue: function(e, t) {
			var n = B();
			return L === null ? $s(n, e, t) : ec(n, L.memoizedState, e, t);
		},
		useTransition: function() {
			var e = _s(ms)[0], t = B().memoizedState;
			return [typeof e == "boolean" ? e : ds(e), t];
		},
		useSyncExternalStore: vs,
		useId: sc,
		useHostTransitionStatus: oc,
		useFormState: Ls,
		useActionState: Ls,
		useOptimistic: function(e, t) {
			var n = B();
			return L === null ? (n.baseState = e, [e, n.queue.dispatch]) : Ts(n, L, e, t);
		},
		useMemoCache: ps,
		useCacheRefresh: cc,
		useEffectEvent: Gs
	};
	function xc(e, t, n, r) {
		t = e.memoizedState, n = n(r, t), n = n == null ? t : E({}, t, n), e.memoizedState = n, e.lanes === 0 && (e.updateQueue.baseState = n);
	}
	var Sc = {
		enqueueSetState: function(e, t, n) {
			e = e._reactInternals;
			var r = jd(), i = So(r);
			i.payload = t, n != null && (i.callback = n), t = Co(e, i, r), t !== null && (Pd(t, e, r), wo(t, e, r));
		},
		enqueueReplaceState: function(e, t, n) {
			e = e._reactInternals;
			var r = jd(), i = So(r);
			i.tag = 1, i.payload = t, n != null && (i.callback = n), t = Co(e, i, r), t !== null && (Pd(t, e, r), wo(t, e, r));
		},
		enqueueForceUpdate: function(e, t) {
			e = e._reactInternals;
			var n = jd(), r = So(n);
			r.tag = 2, t != null && (r.callback = t), t = Co(e, r, n), t !== null && (Pd(t, e, n), wo(t, e, n));
		}
	};
	function Cc(e, t, n, r, i, a, o) {
		return e = e.stateNode, typeof e.shouldComponentUpdate == "function" ? e.shouldComponentUpdate(r, a, o) : t.prototype && t.prototype.isPureReactComponent ? !Jr(n, r) || !Jr(i, a) : !0;
	}
	function wc(e, t, n, r) {
		e = t.state, typeof t.componentWillReceiveProps == "function" && t.componentWillReceiveProps(n, r), typeof t.UNSAFE_componentWillReceiveProps == "function" && t.UNSAFE_componentWillReceiveProps(n, r), t.state !== e && Sc.enqueueReplaceState(t, t.state, null);
	}
	function Tc(e, t) {
		var n = t;
		if ("ref" in t) for (var r in n = {}, t) r !== "ref" && (n[r] = t[r]);
		if (e = e.defaultProps) for (var i in n === t && (n = E({}, n)), e) n[i] === void 0 && (n[i] = e[i]);
		return n;
	}
	function Ec(e) {
		Ei(e);
	}
	function Dc(e) {
		console.error(e);
	}
	function Oc(e) {
		Ei(e);
	}
	function kc(e, t) {
		try {
			var n = e.onUncaughtError;
			n(t.value, { componentStack: t.stack });
		} catch (e) {
			setTimeout(function() {
				throw e;
			});
		}
	}
	function Ac(e, t, n) {
		try {
			var r = e.onCaughtError;
			r(n.value, {
				componentStack: n.stack,
				errorBoundary: t.tag === 1 ? t.stateNode : null
			});
		} catch (e) {
			setTimeout(function() {
				throw e;
			});
		}
	}
	function jc(e, t, n) {
		return n = So(n), n.tag = 3, n.payload = { element: null }, n.callback = function() {
			kc(e, t);
		}, n;
	}
	function Mc(e) {
		return e = So(e), e.tag = 3, e;
	}
	function Nc(e, t, n, r) {
		var i = n.type.getDerivedStateFromError;
		if (typeof i == "function") {
			var a = r.value;
			e.payload = function() {
				return i(a);
			}, e.callback = function() {
				Ac(t, n, r);
			};
		}
		var o = n.stateNode;
		o !== null && typeof o.componentDidCatch == "function" && (e.callback = function() {
			Ac(t, n, r), typeof i != "function" && (yd === null ? yd = /* @__PURE__ */ new Set([this]) : yd.add(this));
			var e = r.stack;
			this.componentDidCatch(r.value, { componentStack: e === null ? "" : e });
		});
	}
	function Pc(e, t, n, r, a) {
		if (n.flags |= 32768, typeof r == "object" && r && typeof r.then == "function") {
			if (t = n.alternate, t !== null && Da(t, n, a, !0), n = Io.current, n !== null) {
				switch (n.tag) {
					case 31:
					case 13:
					case 19: return Lo === null ? Kd() : n.alternate === null && Y === 0 && (Y = 3), n.flags &= -257, n.flags |= 65536, n.lanes = a, r === ro ? n.flags |= 16384 : (t = n.updateQueue, t === null ? n.updateQueue = /* @__PURE__ */ new Set([r]) : t.add(r), mf(e, r, a)), !1;
					case 22: return n.flags |= 65536, r === ro ? n.flags |= 16384 : (t = n.updateQueue, t === null ? (t = {
						transitions: null,
						markerInstances: null,
						retryQueue: /* @__PURE__ */ new Set([r])
					}, n.updateQueue = t) : (n = t.retryQueue, n === null ? t.retryQueue = /* @__PURE__ */ new Set([r]) : n.add(r)), mf(e, r, a)), !1;
				}
				throw Error(i(435, n.tag));
			}
			return mf(e, r, a), Kd(), !1;
		}
		if (F) return t = Io.current, t === null ? (r !== fa && (t = Error(i(423), { cause: r }), ya(Ji(t, n))), e = e.current.alternate, e.flags |= 65536, a &= -a, e.lanes |= a, r = Ji(r, n), a = jc(e.stateNode, r, a), To(e, a), Y !== 4 && (Y = 2)) : (!(t.flags & 65536) && (t.flags |= 256), t.flags |= 65536, t.lanes = a, r !== fa && (e = Error(i(422), { cause: r }), ya(Ji(e, n)))), !1;
		var o = Error(i(520), { cause: r });
		if (o = Ji(o, n), fd === null ? fd = [o] : fd.push(o), Y !== 4 && (Y = 2), t === null) return !0;
		r = Ji(r, n), n = t;
		do {
			switch (n.tag) {
				case 3: return n.flags |= 65536, e = a & -a, n.lanes |= e, e = jc(n.stateNode, r, e), To(n, e), !1;
				case 1:
					if (t = n.type, o = n.stateNode, !(n.flags & 128) && (typeof t.getDerivedStateFromError == "function" || o !== null && typeof o.componentDidCatch == "function" && (yd === null || !yd.has(o)))) return n.flags |= 65536, a &= -a, n.lanes |= a, a = Mc(a), Nc(a, e, n, r), To(n, a), !1;
					break;
				case 22: if (n.memoizedState !== null) return n.flags |= 65536, !1;
			}
			n = n.return;
		} while (n !== null);
		return !1;
	}
	var Fc = Error(i(461)), Ic = !1;
	function Lc(e, t, n, r) {
		t.child = e === null ? vo(t, null, n, r) : _o(t, e.child, n, r);
	}
	function Rc(e, t, n, r, i) {
		n = n.render;
		var a = t.ref;
		if ("ref" in r) {
			var o = {};
			for (var s in r) s !== "ref" && (o[s] = r[s]);
		} else o = r;
		return ka(t), r = ns(e, t, n, o, a, i), s = os(), e !== null && !Ic ? (ss(e, t, i), fl(e, t, i)) : (F && s && oa(t), t.flags |= 1, Lc(e, t, r, i), t.child);
	}
	function zc(e, t, n, r, i) {
		if (e === null) {
			var a = n.type;
			return typeof a == "function" && !zi(a) && a.defaultProps === void 0 && n.compare === null ? (t.tag = 15, t.type = a, Bc(e, t, a, r, i)) : (e = Hi(n.type, null, r, t, t.mode, i), e.ref = t.ref, e.return = t, t.child = e);
		}
		if (a = e.child, !pl(e, i)) {
			var o = a.memoizedProps;
			if (n = n.compare, n = n === null ? Jr : n, n(o, r) && e.ref === t.ref) return fl(e, t, i);
		}
		return t.flags |= 1, e = Bi(a, r), e.ref = t.ref, e.return = t, t.child = e;
	}
	function Bc(e, t, n, r, i) {
		if (e !== null) {
			var a = e.memoizedProps;
			if (Jr(a, r) && e.ref === t.ref) {
				if (Ic = !1, t.pendingProps = r = a, pl(e, i)) e.flags & 131072 && (Ic = !0);
				else return t.lanes = e.lanes, fl(e, t, i);
			}
		}
		return Jc(e, t, n, r, i);
	}
	function Vc(e, t, n, r) {
		var i = r.children, a = e === null ? null : e.memoizedState;
		if (e === null && t.stateNode === null && (t.stateNode = {
			_visibility: 1,
			_pendingMarkers: null,
			_retryCache: null,
			_transitions: null
		}), r.mode === "hidden") {
			if (t.flags & 128) {
				if (a = a === null ? n : a.baseLanes | n, e !== null) {
					for (r = t.child = e.child, i = 0; r !== null;) i = i | r.lanes | r.childLanes, r = r.sibling;
					r = i & ~a;
				} else r = 0, t.child = null;
				return Uc(e, t, a, n, r);
			}
			if (n & 536870912) t.memoizedState = {
				baseLanes: 0,
				cachePool: null
			}, e !== null && Qa(t, a === null ? null : a.cachePool), a === null ? Po() : No(t, a), Bo(t);
			else return r = t.lanes = 536870912, Uc(e, t, a === null ? n : a.baseLanes | n, n, r);
		} else a === null ? (e !== null && Qa(t, null), Po(), Vo()) : (Qa(t, a.cachePool), No(t, a), Vo(), t.memoizedState = null);
		return Lc(e, t, i, n), t.child;
	}
	function Hc(e, t) {
		return e !== null && e.tag === 22 || t.stateNode !== null || (t.stateNode = {
			_visibility: 1,
			_pendingMarkers: null,
			_retryCache: null,
			_transitions: null
		}), t.sibling;
	}
	function Uc(e, t, n, r, i) {
		var a = Za();
		return a = a === null ? null : {
			parent: Ia._currentValue,
			pool: a
		}, t.memoizedState = {
			baseLanes: n,
			cachePool: a
		}, e !== null && Qa(t, null), Po(), Bo(t), e !== null && Da(e, t, r, !0), t.childLanes = i, null;
	}
	function Wc(e, t) {
		return t = rl({
			mode: t.mode,
			children: t.children
		}, e.mode), t.ref = e.ref, e.child = t, t.return = e, t;
	}
	function Gc(e, t, n) {
		return _o(t, e.child, null, n), e = Wc(t, t.pendingProps), e.flags |= 2, Ho(t), t.memoizedState = null, e;
	}
	function Kc(e, t, n) {
		var r = t.pendingProps, a = !!(t.flags & 128);
		if (t.flags &= -129, e === null) {
			if (F) {
				if (r.mode === "hidden") return e = Wc(t, r), t.lanes = 536870912, e.memoizedState = {
					baseLanes: 0,
					cachePool: null
				}, Hc(null, e);
				if (zo(t), (e = P) ? (e = am(e, da), e = e !== null && e.data === "&" ? e : null, e !== null && (t.memoizedState = {
					dehydrated: e,
					treeContext: ta === null ? null : {
						id: na,
						overflow: ra
					},
					retryLane: 536870912,
					hydrationErrors: null
				}, n = Gi(e), n.return = t, t.child = n, la = t, P = null)) : e = null, e === null) throw pa(t);
				return t.lanes = 536870912, null;
			}
			return Wc(t, r);
		}
		var o = e.memoizedState;
		if (o !== null) {
			var s = o.dehydrated;
			if (zo(t), a) {
				if (t.flags & 256) t.flags &= -257, t = Gc(e, t, n);
				else if (t.memoizedState !== null) t.child = e.child, t.flags |= 128, t = null;
				else throw Error(i(558));
			} else if (Ic || Da(e, t, n, !1), a = (n & e.childLanes) !== 0, Ic || a) {
				if (jo.current === null) {
					if (r = G, r !== null && (s = St(r, n), s !== 0 && s !== o.retryLane)) throw o.retryLane = s, Ni(e, s), Pd(r, e, s), Fc;
					Kd();
				}
				t = Gc(e, t, n);
			} else e = o.treeContext, P = lm(s.nextSibling), la = t, F = !0, ua = null, da = !1, e !== null && ca(t, e), t = Wc(t, r), t.flags |= 134221824;
			return t;
		}
		return e = Bi(e.child, {
			mode: r.mode,
			children: r.children
		}), e.ref = t.ref, t.child = e, e.return = t, e;
	}
	function qc(e, t) {
		var n = t.ref;
		if (n === null) e !== null && e.ref !== null && (t.flags |= 4194816);
		else {
			if (typeof n != "function" && typeof n != "object") throw Error(i(284));
			(e === null || e.ref !== n) && (t.flags |= 4194816);
		}
	}
	function Jc(e, t, n, r, i) {
		return ka(t), n = ns(e, t, n, r, void 0, i), r = os(), e !== null && !Ic ? (ss(e, t, i), fl(e, t, i)) : (F && r && oa(t), t.flags |= 1, Lc(e, t, n, i), t.child);
	}
	function Yc(e, t, n, r, i, a) {
		return ka(t), t.updateQueue = null, n = is(t, r, n, i), rs(e), r = os(), e !== null && !Ic ? (ss(e, t, a), fl(e, t, a)) : (F && r && oa(t), t.flags |= 1, Lc(e, t, n, a), t.child);
	}
	function Xc(e, t, n, r, i) {
		if (ka(t), t.stateNode === null) {
			var a = Ii, o = n.contextType;
			typeof o == "object" && o && (a = Aa(o)), a = new n(r, a), t.memoizedState = a.state !== null && a.state !== void 0 ? a.state : null, a.updater = Sc, t.stateNode = a, a._reactInternals = t, a = t.stateNode, a.props = r, a.state = t.memoizedState, a.refs = {}, bo(t), o = n.contextType, a.context = typeof o == "object" && o ? Aa(o) : Ii, a.state = t.memoizedState, o = n.getDerivedStateFromProps, typeof o == "function" && (xc(t, n, o, r), a.state = t.memoizedState), typeof n.getDerivedStateFromProps == "function" || typeof a.getSnapshotBeforeUpdate == "function" || typeof a.UNSAFE_componentWillMount != "function" && typeof a.componentWillMount != "function" || (o = a.state, typeof a.componentWillMount == "function" && a.componentWillMount(), typeof a.UNSAFE_componentWillMount == "function" && a.UNSAFE_componentWillMount(), o !== a.state && Sc.enqueueReplaceState(a, a.state, null), Oo(t, r, a, i), Do(), a.state = t.memoizedState), typeof a.componentDidMount == "function" && (t.flags |= 4194308), r = !0;
		} else if (e === null) {
			a = t.stateNode;
			var s = t.memoizedProps, c = Tc(n, s);
			a.props = c;
			var l = a.context, u = n.contextType;
			o = Ii, typeof u == "object" && u && (o = Aa(u));
			var d = n.getDerivedStateFromProps;
			u = typeof d == "function" || typeof a.getSnapshotBeforeUpdate == "function", s = t.pendingProps !== s, u || typeof a.UNSAFE_componentWillReceiveProps != "function" && typeof a.componentWillReceiveProps != "function" || (s || l !== o) && wc(t, a, r, o), yo = !1;
			var f = t.memoizedState;
			a.state = f, Oo(t, r, a, i), Do(), l = t.memoizedState, s || f !== l || yo ? (typeof d == "function" && (xc(t, n, d, r), l = t.memoizedState), (c = yo || Cc(t, n, c, r, f, l, o)) ? (u || typeof a.UNSAFE_componentWillMount != "function" && typeof a.componentWillMount != "function" || (typeof a.componentWillMount == "function" && a.componentWillMount(), typeof a.UNSAFE_componentWillMount == "function" && a.UNSAFE_componentWillMount()), typeof a.componentDidMount == "function" && (t.flags |= 4194308)) : (typeof a.componentDidMount == "function" && (t.flags |= 4194308), t.memoizedProps = r, t.memoizedState = l), a.props = r, a.state = l, a.context = o, r = c) : (typeof a.componentDidMount == "function" && (t.flags |= 4194308), r = !1);
		} else {
			a = t.stateNode, xo(e, t), o = t.memoizedProps, u = Tc(n, o), a.props = u, d = t.pendingProps, f = a.context, l = n.contextType, c = Ii, typeof l == "object" && l && (c = Aa(l)), s = n.getDerivedStateFromProps, (l = typeof s == "function" || typeof a.getSnapshotBeforeUpdate == "function") || typeof a.UNSAFE_componentWillReceiveProps != "function" && typeof a.componentWillReceiveProps != "function" || (o !== d || f !== c) && wc(t, a, r, c), yo = !1, f = t.memoizedState, a.state = f, Oo(t, r, a, i), Do();
			var p = t.memoizedState;
			o !== d || f !== p || yo || e !== null && e.dependencies !== null && Oa(e.dependencies) ? (typeof s == "function" && (xc(t, n, s, r), p = t.memoizedState), (u = yo || Cc(t, n, u, r, f, p, c) || e !== null && e.dependencies !== null && Oa(e.dependencies)) ? (l || typeof a.UNSAFE_componentWillUpdate != "function" && typeof a.componentWillUpdate != "function" || (typeof a.componentWillUpdate == "function" && a.componentWillUpdate(r, p, c), typeof a.UNSAFE_componentWillUpdate == "function" && a.UNSAFE_componentWillUpdate(r, p, c)), typeof a.componentDidUpdate == "function" && (t.flags |= 4), typeof a.getSnapshotBeforeUpdate == "function" && (t.flags |= 1024)) : (typeof a.componentDidUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 4), typeof a.getSnapshotBeforeUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 1024), t.memoizedProps = r, t.memoizedState = p), a.props = r, a.state = p, a.context = c, r = u) : (typeof a.componentDidUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 4), typeof a.getSnapshotBeforeUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 1024), r = !1);
		}
		return a = r, qc(e, t), r = !!(t.flags & 128), a || r ? (a = t.stateNode, n = r && typeof n.getDerivedStateFromError != "function" ? null : a.render(), t.flags |= 1, e !== null && r ? (t.child = _o(t, e.child, null, i), t.child = _o(t, null, n, i)) : Lc(e, t, n, i), t.memoizedState = a.state, e = t.child) : e = fl(e, t, i), e;
	}
	function Zc(e, t, n, r) {
		return _a(), t.flags |= 256, Lc(e, t, n, r), t.child;
	}
	var Qc = {
		dehydrated: null,
		treeContext: null,
		retryLane: 0,
		hydrationErrors: null
	};
	function $c(e) {
		return {
			baseLanes: e,
			cachePool: $a()
		};
	}
	function el(e, t, n) {
		return e = e === null ? 0 : e.childLanes & ~n, t && (e |= ud), e;
	}
	function tl(e, t, n) {
		var r = t.pendingProps, i = !1, a = !!(t.flags & 128), o;
		if ((o = a) || (o = e !== null && e.memoizedState === null ? !1 : !!(Uo.current & 2)), o && (i = !0, t.flags &= -129), o = !!(t.flags & 32), t.flags &= -33, e === null) {
			if (F) {
				if (i ? Ro(t) : Vo(), (e = P) ? (e = am(e, da), e = e !== null && e.data !== "&" ? e : null, e !== null && (t.memoizedState = {
					dehydrated: e,
					treeContext: ta === null ? null : {
						id: na,
						overflow: ra
					},
					retryLane: 536870912,
					hydrationErrors: null
				}, n = Gi(e), n.return = t, t.child = n, la = t, P = null)) : e = null, e === null) throw pa(t);
				return t.lanes = sm(e) ? 32 : 536870912, null;
			}
			return a = r.children, r = r.fallback, i ? (Vo(), i = t.mode, a = rl({
				mode: "hidden",
				children: a
			}, i), r = Ui(r, i, n, null), a.return = t, r.return = t, a.sibling = r, t.child = a, r = t.child, r.memoizedState = $c(n), r.childLanes = el(e, o, n), t.memoizedState = Qc, Hc(null, r)) : (Ro(t), nl(t, a));
		}
		var s = e.memoizedState;
		if (s !== null) {
			var c = s.dehydrated;
			if (c !== null) return al(e, t, a, o, r, c, s, n);
		}
		return i ? (Vo(), i = r.fallback, a = t.mode, s = e.child, c = s.sibling, r = Bi(s, {
			mode: "hidden",
			children: r.children
		}), r.subtreeFlags = s.subtreeFlags & 1206910976, c === null ? (i = Ui(i, a, n, null), i.flags |= 2) : i = Bi(c, i), i.return = t, r.return = t, r.sibling = i, t.child = r, Hc(null, r), r = t.child, i = e.child.memoizedState, i === null ? i = $c(n) : (a = i.cachePool, a === null ? a = $a() : (s = Ia._currentValue, a = a.parent === s ? a : {
			parent: s,
			pool: s
		}), i = {
			baseLanes: i.baseLanes | n,
			cachePool: a
		}), r.memoizedState = i, r.childLanes = el(e, o, n), t.memoizedState = Qc, Hc(e.child, r)) : (Ro(t), n = e.child, e = n.sibling, n = Bi(n, {
			mode: "visible",
			children: r.children
		}), n.return = t, n.sibling = null, e !== null && (o = t.deletions, o === null ? (t.deletions = [e], t.flags |= 16) : o.push(e)), t.child = n, t.memoizedState = null, n);
	}
	function nl(e, t) {
		return t = rl({
			mode: "visible",
			children: t
		}, e.mode), t.return = e, e.child = t;
	}
	function rl(e, t) {
		return e = Ri(22, e, null, t), e.lanes = 0, e;
	}
	function il(e, t, n) {
		return _o(t, e.child, null, n), e = nl(t, t.pendingProps.children), e.flags |= 2, t.memoizedState = null, e;
	}
	function al(e, t, n, r, a, o, s, c) {
		if (n) return t.flags & 256 ? (Ro(t), t.flags &= -257, il(e, t, c)) : t.memoizedState === null ? (Vo(), o = a.fallback, s = t.mode, a = rl({
			mode: "visible",
			children: a.children
		}, s), o = Ui(o, s, c, null), o.flags |= 2, a.return = t, o.return = t, a.sibling = o, t.child = a, _o(t, e.child, null, c), a = t.child, a.memoizedState = $c(c), a.childLanes = el(e, r, c), t.memoizedState = Qc, Hc(null, a)) : (Vo(), t.child = e.child, t.flags |= 128, null);
		if (Ro(t), sm(o)) {
			if (r = o.nextSibling && o.nextSibling.dataset, r) var l = r.dgst;
			return r = l, r !== "" && (a = Error(i(419)), a.stack = "", a.digest = r, ya({
				value: a,
				source: null,
				stack: null
			})), il(e, t, c);
		}
		if (Ic || Da(e, t, c, !1), r = (c & e.childLanes) !== 0, Ic || r) {
			if (jo.current !== null) return il(e, t, c);
			if (r = G, r !== null && (a = St(r, c), a !== 0 && a !== s.retryLane)) throw s.retryLane = a, Ni(e, a), Pd(r, e, a), Fc;
			return om(o) || Kd(), il(e, t, c);
		}
		return om(o) ? (t.flags |= 192, t.child = e.child, null) : (e = s.treeContext, P = lm(o.nextSibling), la = t, F = !0, ua = null, da = !1, e !== null && ca(t, e), t = nl(t, a.children), t.flags |= 134221824, t);
	}
	function ol(e, t, n) {
		e.lanes |= t;
		var r = e.alternate;
		r !== null && (r.lanes |= t), Ta(e.return, t, n);
	}
	function sl(e) {
		for (var t = null; e !== null;) {
			var n = e.alternate;
			n !== null && Ko(n) === null && (t = e), e = e.sibling;
		}
		return t;
	}
	function cl(e, t, n, r, i, a) {
		var o = e.memoizedState;
		o === null ? e.memoizedState = {
			isBackwards: t,
			rendering: null,
			renderingStartTime: 0,
			last: r,
			tail: n,
			tailMode: i,
			treeForkCount: a
		} : (o.isBackwards = t, o.rendering = null, o.renderingStartTime = 0, o.last = r, o.tail = n, o.tailMode = i, o.treeForkCount = a);
	}
	function ll(e) {
		var t = e.child;
		for (e.child = null; t !== null;) {
			var n = t.sibling;
			t.sibling = e.child, e.child = t, t = n;
		}
	}
	function ul(e, t, n) {
		var r = t.pendingProps, i = r.revealOrder, a = r.tail;
		r = r.children;
		var o = Uo.current;
		if (t.flags & 128) return Wo(t, o), null;
		var s = !!(o & 2);
		if (s ? (o = o & 1 | 2, t.flags |= 128) : o &= 1, Wo(t, o), i === "backwards" && e !== null ? (ll(e), Lc(e, t, r, n), ll(e)) : Lc(e, t, r, n), r = F ? Qi : 0, !s && e !== null && e.flags & 128) a: for (e = t.child; e !== null;) {
			if (e.tag === 13) e.memoizedState !== null && ol(e, n, t);
			else if (e.tag === 19) ol(e, n, t);
			else if (e.child !== null) {
				e.child.return = e, e = e.child;
				continue;
			}
			if (e === t) break a;
			for (; e.sibling === null;) {
				if (e.return === null || e.return === t) break a;
				e = e.return;
			}
			e.sibling.return = e.return, e = e.sibling;
		}
		switch (i) {
			case "backwards":
				n = sl(t.child), n === null ? (i = t.child, t.child = null) : (i = n.sibling, n.sibling = null, ll(t)), cl(t, !0, i, null, a, r);
				break;
			case "unstable_legacy-backwards":
				for (n = null, i = t.child, t.child = null; i !== null;) {
					if (e = i.alternate, e !== null && Ko(e) === null) {
						t.child = i;
						break;
					}
					e = i.sibling, i.sibling = n, n = i, i = e;
				}
				cl(t, !0, n, null, a, r);
				break;
			case "together":
				cl(t, !1, null, null, void 0, r);
				break;
			case "independent":
				t.memoizedState = null;
				break;
			default: n = sl(t.child), n === null ? (i = t.child, t.child = null) : (i = n.sibling, n.sibling = null), cl(t, !1, i, n, a, r);
		}
		return t.child;
	}
	function dl(e, t, n) {
		var r = t.pendingProps;
		return Ca(t, t.type, r.value), Lc(e, t, r.children, n), t.child;
	}
	function fl(e, t, n) {
		if (e !== null && (t.dependencies = e.dependencies), sd |= t.lanes, (n & t.childLanes) === 0) {
			if (e !== null) {
				if (Da(e, t, n, !1), (n & t.childLanes) === 0) return null;
			} else return null;
		}
		if (e !== null && t.child !== e.child) throw Error(i(153));
		if (t.child !== null) {
			for (e = t.child, n = Bi(e, e.pendingProps), t.child = n, n.return = t; e.sibling !== null;) e = e.sibling, n = n.sibling = Bi(e, e.pendingProps), n.return = t;
			n.sibling = null;
		}
		return t.child;
	}
	function pl(e, t) {
		return (e.lanes & t) !== 0 || (e = e.dependencies, !!(e !== null && Oa(e)));
	}
	function ml(e, t, n) {
		switch (t.tag) {
			case 3:
				je(t, t.stateNode.containerInfo), Ca(t, Ia, e.memoizedState.cache), _a();
				break;
			case 27:
			case 5:
				Ne(t);
				break;
			case 4:
				je(t, t.stateNode.containerInfo);
				break;
			case 10:
				Ca(t, t.type, t.memoizedProps.value);
				break;
			case 31:
				if (t.memoizedState !== null) return t.flags |= 128, zo(t), null;
				break;
			case 13:
				var r = t.memoizedState;
				if (r !== null) {
					if (r.dehydrated !== null) return Ro(t), t.flags |= 128, null;
					r = Da(e, t, n, !1);
					var i = t.child.childLanes;
					return r || (n & i) !== 0 ? tl(e, t, n) : (Ro(t), e = fl(e, t, n), e === null ? null : e.sibling);
				}
				Ro(t);
				break;
			case 19:
				if (t.flags & 128) return ul(e, t, n);
				if (i = !!(e.flags & 128), r = (n & t.childLanes) !== 0, r ||= (Da(e, t, n, !1), (n & t.childLanes) !== 0), i) {
					if (r) return ul(e, t, n);
					t.flags |= 128;
				}
				if (i = t.memoizedState, i !== null && (i.rendering = null, i.tail = null, i.lastEffect = null), Wo(t, Uo.current), r) break;
				return null;
			case 22: return t.lanes = 0, Vc(e, t, n, t.pendingProps);
			case 24: Ca(t, Ia, e.memoizedState.cache);
		}
		return fl(e, t, n);
	}
	function hl(e, t, n) {
		if (e !== null) {
			if (e.memoizedProps !== t.pendingProps) Ic = !0;
			else {
				if (!pl(e, n) && !(t.flags & 128)) return Ic = !1, ml(e, t, n);
				Ic = !!(e.flags & 131072);
			}
		} else Ic = !1, F && t.flags & 1048576 && aa(t, Qi, t.index);
		switch (t.lanes = 0, t.tag) {
			case 16:
				a: {
					var r = t.pendingProps;
					if (e = oo(t.elementType), t.type = e, typeof e == "function") zi(e) ? (r = Tc(e, r), t.tag = 1, t = Xc(null, t, e, r, n)) : (t.tag = 0, t = Jc(null, t, e, r, n));
					else {
						if (e != null) {
							var a = e.$$typeof;
							if (a === ce) {
								t.tag = 11, t = Rc(null, t, e, r, n);
								break a;
							}
							if (a === de) {
								t.tag = 14, t = zc(null, t, e, r, n);
								break a;
							}
							if (a === se) {
								t.tag = 10, t.type = e, t = dl(null, t, n);
								break a;
							}
						}
						throw t = be(e) || e, Error(i(306, t, ""));
					}
				}
				return t;
			case 0: return Jc(e, t, t.type, t.pendingProps, n);
			case 1: return r = t.type, a = Tc(r, t.pendingProps), Xc(e, t, r, a, n);
			case 3:
				a: {
					if (je(t, t.stateNode.containerInfo), e === null) throw Error(i(387));
					r = t.pendingProps;
					var o = t.memoizedState;
					a = o.element, xo(e, t), Oo(t, r, null, n);
					var s = t.memoizedState;
					if (r = s.cache, Ca(t, Ia, r), r !== o.cache && Ea(t, [Ia], n, !0), Do(), r = s.element, o.isDehydrated) {
						if (o = {
							element: r,
							isDehydrated: !1,
							cache: s.cache
						}, t.updateQueue.baseState = o, t.memoizedState = o, t.flags & 256) {
							t = Zc(e, t, r, n);
							break a;
						}
						if (r !== a) {
							a = Ji(Error(i(424)), t), ya(a), t = Zc(e, t, r, n);
							break a;
						}
						switch (e = t.stateNode.containerInfo, e.nodeType) {
							case 9:
								e = e.body;
								break;
							default: e = e.nodeName === "HTML" ? e.ownerDocument.body : e;
						}
						for (P = lm(e.firstChild), la = t, F = !0, ua = null, da = !0, n = vo(t, null, r, n), t.child = n; n;) n.flags = n.flags & -3 | 134221824, n = n.sibling;
					} else {
						if (_a(), r === a) {
							t = fl(e, t, n);
							break a;
						}
						Lc(e, t, r, n);
					}
					t = t.child;
				}
				return t;
			case 26: return qc(e, t), e === null ? (n = Nm(t.type, null, t.pendingProps, null)) ? t.memoizedState = n : F || (t.stateNode = fp(t.type, t.pendingProps, ke.current, t)) : t.memoizedState = Nm(t.type, e.memoizedProps, t.pendingProps, e.memoizedState), null;
			case 27: return Ne(t), e === null && F && (r = t.stateNode = hm(t.type, t.pendingProps, ke.current), la = t, da = !0, a = P, Sp(t.type) ? (um = a, P = lm(r.firstChild)) : P = a), Lc(e, t, t.pendingProps.children, n), qc(e, t), e === null && (t.flags |= 4194304), t.child;
			case 5: return e === null && F && ((a = r = P) && (r = rm(r, t.type, t.pendingProps, da), r === null ? a = !1 : (t.stateNode = r, la = t, P = lm(r.firstChild), da = !1, a = !0)), a || pa(t)), Ne(t), a = t.type, o = t.pendingProps, s = e === null ? null : e.memoizedProps, r = o.children, pp(a, o) ? r = null : s !== null && pp(a, s) && (t.flags |= 32), t.memoizedState !== null && (a = ns(e, t, as, null, null, n), sh._currentValue = a), qc(e, t), Lc(e, t, r, n), t.child;
			case 6: return e === null && F && ((e = n = P) && (n = im(n, t.pendingProps, da), n === null ? e = !1 : (t.stateNode = n, la = t, P = null, e = !0)), e || pa(t)), null;
			case 13: return tl(e, t, n);
			case 4: return je(t, t.stateNode.containerInfo), r = t.pendingProps, e === null ? t.child = _o(t, null, r, n) : Lc(e, t, r, n), t.child;
			case 11: return Rc(e, t, t.type, t.pendingProps, n);
			case 7: return r = t.pendingProps, qc(e, t), Lc(e, t, r, n), t.child;
			case 8: return Lc(e, t, t.pendingProps.children, n), t.child;
			case 12: return Lc(e, t, t.pendingProps.children, n), t.child;
			case 10: return dl(e, t, n);
			case 9: return a = t.type._context, r = t.pendingProps.children, ka(t), a = Aa(a), r = r(a), t.flags |= 1, Lc(e, t, r, n), t.child;
			case 14: return zc(e, t, t.type, t.pendingProps, n);
			case 15: return Bc(e, t, t.type, t.pendingProps, n);
			case 19: return ul(e, t, n);
			case 31: return Kc(e, t, n);
			case 22: return Vc(e, t, n, t.pendingProps);
			case 24: return ka(t), r = Aa(Ia), e === null ? (a = Za(), a === null && (a = G, o = La(), a.pooledCache = o, o.refCount++, o !== null && (a.pooledCacheLanes |= n), a = o), t.memoizedState = {
				parent: r,
				cache: a
			}, bo(t), Ca(t, Ia, a)) : ((e.lanes & n) !== 0 && (xo(e, t), Oo(t, null, null, n), Do()), a = e.memoizedState, o = t.memoizedState, a.parent === r ? (r = o.cache, Ca(t, Ia, r), r !== a.cache && Ea(t, [Ia], n, !0)) : (a = {
				parent: r,
				cache: r
			}, t.memoizedState = a, t.lanes === 0 && (t.memoizedState = t.updateQueue.baseState = a), Ca(t, Ia, r))), Lc(e, t, t.pendingProps.children, n), t.child;
			case 30: return t.stateNode === null && (t.stateNode = {
				autoName: null,
				paired: null,
				clones: null,
				ref: null
			}), r = t.pendingProps, r.name != null && r.name !== "auto" ? t.flags |= e === null ? 18882560 : 18874368 : F && oa(t), e !== null && e.memoizedProps.name !== r.name ? t.flags |= 4194816 : qc(e, t), Lc(e, t, r.children, n), t.child;
			case 29: throw t.pendingProps;
		}
		throw Error(i(156, t.tag));
	}
	function gl(e) {
		e.flags |= 4;
	}
	function _l(e, t, n, r, i) {
		var a;
		if ((a = !!(e.mode & 32)) && (a = n === null ? Jm(t, r) : Jm(t, r) && (r.src !== n.src || r.srcSet !== n.srcSet)), a) {
			if (e.flags |= 16777216, (i & 335544128) === i) {
				if (e.stateNode.complete) e.flags |= 8192;
				else if (Ud()) e.flags |= 8192;
				else throw so = ro, to;
			}
		} else e.flags &= -16777217;
	}
	function vl(e, t) {
		if (t.type !== "stylesheet" || t.state.loading & 4) e.flags &= -16777217;
		else if (e.flags |= 16777216, !Ym(t)) {
			if (Ud()) e.flags |= 8192;
			else throw so = ro, to;
		}
	}
	function yl(e, t) {
		t !== null && (e.flags |= 4), e.flags & 16384 && (t = e.tag === 22 ? 536870912 : _t(), e.lanes |= t, dd |= t);
	}
	function bl(e, t) {
		if (!F) switch (e.tailMode) {
			case "visible": break;
			case "collapsed":
				for (var n = e.tail, r = null; n !== null;) n.alternate !== null && (r = n), n = n.sibling;
				r === null ? t || e.tail === null ? e.tail = null : e.tail.sibling = null : r.sibling = null;
				break;
			default:
				for (t = e.tail, n = null; t !== null;) t.alternate !== null && (n = t), t = t.sibling;
				n === null ? e.tail = null : n.sibling = null;
		}
	}
	function V(e) {
		var t = e.alternate !== null && e.alternate.child === e.child, n = 0, r = 0;
		if (t) for (var i = e.child; i !== null;) n |= i.lanes | i.childLanes, r |= i.subtreeFlags & 1206910976, r |= i.flags & 1206910976, i.return = e, i = i.sibling;
		else for (i = e.child; i !== null;) n |= i.lanes | i.childLanes, r |= i.subtreeFlags, r |= i.flags, i.return = e, i = i.sibling;
		return e.subtreeFlags |= r, e.childLanes = n, t;
	}
	function xl(e, t, n) {
		var r = t.pendingProps;
		switch (sa(t), t.tag) {
			case 16:
			case 15:
			case 0:
			case 11:
			case 7:
			case 8:
			case 12:
			case 9:
			case 14: return V(t), null;
			case 1: return V(t), null;
			case 3: return n = t.stateNode, r = null, e !== null && (r = e.memoizedState.cache), t.memoizedState.cache !== r && (t.flags |= 2048), wa(Ia), Me(), n.pendingContext && (n.context = n.pendingContext, n.pendingContext = null), (e === null || e.child === null) && (ga(t) ? gl(t) : e === null || e.memoizedState.isDehydrated && !(t.flags & 256) || (t.flags |= 1024, va())), V(t), null;
			case 26:
				var a = t.type, o = t.memoizedState;
				return e === null ? (gl(t), o === null ? (V(t), _l(t, a, null, r, n)) : (V(t), vl(t, o))) : o ? o === e.memoizedState ? (V(t), t.flags &= -16777217) : (gl(t), V(t), vl(t, o)) : (e = e.memoizedProps, e !== r && gl(t), V(t), _l(t, a, e, r, n)), null;
			case 27:
				if (Pe(t), n = ke.current, a = t.type, e !== null && t.stateNode != null) e.memoizedProps !== r && gl(t);
				else {
					if (!r) {
						if (t.stateNode === null) throw Error(i(166));
						return V(t), t.subtreeFlags &= -33554433, null;
					}
					e = De.current, ga(t) ? ma(t, e) : (e = hm(a, r, n), t.stateNode = e, gl(t));
				}
				return V(t), t.subtreeFlags &= -33554433, null;
			case 5:
				if (Pe(t), a = t.type, e !== null && t.stateNode != null) e.memoizedProps !== r && gl(t);
				else {
					if (!r) {
						if (t.stateNode === null) throw Error(i(166));
						return V(t), t.subtreeFlags &= -33554433, null;
					}
					if (o = De.current, ga(t)) ma(t, o);
					else {
						var s = lp(ke.current);
						switch (o) {
							case 1:
								o = s.createElementNS("http://www.w3.org/2000/svg", a);
								break;
							case 2:
								o = s.createElementNS("http://www.w3.org/1998/Math/MathML", a);
								break;
							default: switch (a) {
								case "svg":
									o = s.createElementNS("http://www.w3.org/2000/svg", a);
									break;
								case "math":
									o = s.createElementNS("http://www.w3.org/1998/Math/MathML", a);
									break;
								case "script":
									o = s.createElement("div"), o.innerHTML = "<script><\/script>", o = o.removeChild(o.firstChild);
									break;
								case "select":
									o = typeof r.is == "string" ? s.createElement("select", { is: r.is }) : s.createElement("select"), r.multiple ? o.multiple = !0 : r.size && (o.size = r.size);
									break;
								default: o = typeof r.is == "string" ? s.createElement(a, { is: r.is }) : s.createElement(a);
							}
						}
						o[Ot] = t, o[kt] = r;
						a: for (s = t.child; s !== null;) {
							if (s.tag === 5 || s.tag === 6) o.appendChild(s.stateNode);
							else if (s.tag !== 4 && s.tag !== 27 && s.child !== null) {
								s.child.return = s, s = s.child;
								continue;
							}
							if (s === t) break a;
							for (; s.sibling === null;) {
								if (s.return === null || s.return === t) break a;
								s = s.return;
							}
							s.sibling.return = s.return, s = s.sibling;
						}
						t.stateNode = o;
						a: switch (np(o, a, r), a) {
							case "button":
							case "input":
							case "select":
							case "textarea":
								r = !!r.autoFocus;
								break a;
							case "img":
								r = !0;
								break a;
							default: r = !1;
						}
						r && gl(t);
					}
				}
				return V(t), t.subtreeFlags &= -33554433, _l(t, t.type, e === null ? null : e.memoizedProps, t.pendingProps, n), null;
			case 6:
				if (e && t.stateNode != null) e.memoizedProps !== r && gl(t);
				else {
					if (typeof r != "string" && t.stateNode === null) throw Error(i(166));
					if (e = ke.current, ga(t)) {
						if (e = t.stateNode, n = t.memoizedProps, r = null, a = la, a !== null) switch (a.tag) {
							case 27:
							case 5: r = a.memoizedProps;
						}
						e[Ot] = t, e = !!(e.nodeValue === n || r !== null && !0 === r.suppressHydrationWarning || ep(e.nodeValue, n)), e || pa(t, !0);
					} else e = lp(e).createTextNode(r), e[Ot] = t, t.stateNode = e;
				}
				return V(t), null;
			case 31:
				if (n = t.memoizedState, e === null || e.memoizedState !== null) {
					if (r = ga(t), n !== null) {
						if (e === null) {
							if (!r) throw Error(i(318));
							if (e = t.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(557));
							e[Ot] = t;
						} else _a(), !(t.flags & 128) && (t.memoizedState = null), t.flags |= 4;
						V(t), e = !1;
					} else n = va(), e !== null && e.memoizedState !== null && (e.memoizedState.hydrationErrors = n), e = !0;
					if (!e) return t.flags & 256 ? (Ho(t), t) : (Ho(t), null);
					if (t.flags & 128) throw Error(i(558));
				}
				return V(t), null;
			case 13:
				if (r = t.memoizedState, e === null || e.memoizedState !== null && e.memoizedState.dehydrated !== null) {
					if (a = ga(t), r !== null && r.dehydrated !== null) {
						if (e === null) {
							if (!a) throw Error(i(318));
							if (a = t.memoizedState, a = a === null ? null : a.dehydrated, !a) throw Error(i(317));
							a[Ot] = t;
						} else _a(), !(t.flags & 128) && (t.memoizedState = null), t.flags |= 4;
						V(t), a = !1;
					} else a = va(), e !== null && e.memoizedState !== null && (e.memoizedState.hydrationErrors = a), a = !0;
					if (!a) return t.flags & 256 ? (Ho(t), t) : (Ho(t), null);
				}
				return Ho(t), t.flags & 128 ? (t.lanes = n, t) : (n = r !== null, e = e !== null && e.memoizedState !== null, n && (r = t.child, a = null, r.alternate !== null && r.alternate.memoizedState !== null && r.alternate.memoizedState.cachePool !== null && (a = r.alternate.memoizedState.cachePool.pool), o = null, r.memoizedState !== null && r.memoizedState.cachePool !== null && (o = r.memoizedState.cachePool.pool), o !== a && (r.flags |= 2048)), n !== e && n && (t.child.flags |= 8192), yl(t, t.updateQueue), V(t), null);
			case 4: return Me(), e === null && Wf(t.stateNode.containerInfo), t.flags |= 67108864, V(t), null;
			case 10: return wa(t.type), V(t), null;
			case 19:
				if (Go(t), r = t.memoizedState, r === null) return V(t), null;
				if (a = !!(t.flags & 128), o = r.rendering, o === null) {
					if (a) bl(r, !1);
					else {
						if (Y !== 0 || e !== null && e.flags & 128) for (e = t.child; e !== null;) {
							if (o = Ko(e), o !== null) {
								for (t.flags |= 128, bl(r, !1), e = o.updateQueue, t.updateQueue = e, yl(t, e), t.subtreeFlags = 0, e = n, n = t.child; n !== null;) Vi(n, e), n = n.sibling;
								return Wo(t, Uo.current & 1 | 2), F && ia(t, r.treeForkCount), t.child;
							}
							e = e.sibling;
						}
						r.tail !== null && qe() > _d && (t.flags |= 128, a = !0, bl(r, !1), t.lanes = 4194304);
					}
				} else {
					if (!a) {
						if (e = Ko(o), e !== null) {
							if (t.flags |= 128, a = !0, e = e.updateQueue, t.updateQueue = e, yl(t, e), bl(r, !0), r.tail === null && r.tailMode !== "collapsed" && r.tailMode !== "visible" && !o.alternate && !F) return V(t), null;
						} else 2 * qe() - r.renderingStartTime > _d && n !== 536870912 && (t.flags |= 128, a = !0, bl(r, !1), t.lanes = 4194304);
					}
					r.isBackwards ? (o.sibling = t.child, t.child = o) : (e = r.last, e === null ? t.child = o : e.sibling = o, r.last = o);
				}
				if (r.tail !== null) {
					e = r.tail;
					a: {
						for (n = e; n !== null;) {
							if (n.alternate !== null) {
								n = !1;
								break a;
							}
							n = n.sibling;
						}
						n = !0;
					}
					return r.rendering = e, r.tail = e.sibling, r.renderingStartTime = qe(), e.sibling = null, o = Uo.current, o = a ? o & 1 | 2 : o & 1, r.tailMode === "visible" || r.tailMode === "collapsed" || !n || F ? Wo(t, o) : (n = o, j(Io, t), j(Uo, n), Lo === null && (Lo = t)), F && ia(t, r.treeForkCount), e;
				}
				return V(t), null;
			case 22:
			case 23: return Ho(t), Fo(), r = t.memoizedState !== null, e === null ? r && (t.flags |= 8192) : e.memoizedState !== null !== r && (t.flags |= 8192), r ? n & 536870912 && !(t.flags & 128) && (V(t), t.subtreeFlags & 6 && (t.flags |= 8192)) : V(t), n = t.updateQueue, n !== null && yl(t, n.retryQueue), n = null, e !== null && e.memoizedState !== null && e.memoizedState.cachePool !== null && (n = e.memoizedState.cachePool.pool), r = null, t.memoizedState !== null && t.memoizedState.cachePool !== null && (r = t.memoizedState.cachePool.pool), r !== n && (t.flags |= 2048), e !== null && Ee(Xa), null;
			case 24: return n = null, e !== null && (n = e.memoizedState.cache), t.memoizedState.cache !== n && (t.flags |= 2048), wa(Ia), V(t), null;
			case 25: return null;
			case 30: return t.flags |= 33554432, V(t), null;
		}
		throw Error(i(156, t.tag));
	}
	function Sl(e, t) {
		switch (sa(t), t.tag) {
			case 1: return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 3: return wa(Ia), Me(), e = t.flags, e & 65536 && !(e & 128) ? (t.flags = e & -65537 | 128, t) : null;
			case 26:
			case 27:
			case 5: return Pe(t), null;
			case 31:
				if (t.memoizedState !== null) {
					if (Ho(t), t.alternate === null) throw Error(i(340));
					_a();
				}
				return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 13:
				if (Ho(t), e = t.memoizedState, e !== null && e.dehydrated !== null) {
					if (t.alternate === null) throw Error(i(340));
					_a();
				}
				return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 19: return Go(t), e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, e = t.memoizedState, e !== null && (e.rendering = null, e.tail = null), t.flags |= 4, t) : null;
			case 4: return Me(), null;
			case 10: return wa(t.type), null;
			case 22:
			case 23: return Ho(t), Fo(), e !== null && Ee(Xa), e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 24: return wa(Ia), null;
			case 25: return null;
			default: return null;
		}
	}
	function Cl(e, t) {
		switch (sa(t), t.tag) {
			case 3:
				wa(Ia), Me();
				break;
			case 26:
			case 27:
			case 5:
				Pe(t);
				break;
			case 4:
				Me();
				break;
			case 31:
				t.memoizedState !== null && Ho(t);
				break;
			case 13:
				Ho(t);
				break;
			case 19:
				Go(t);
				break;
			case 10:
				wa(t.type);
				break;
			case 22:
			case 23:
				Ho(t), Fo(), e !== null && Ee(Xa);
				break;
			case 24: wa(Ia);
		}
	}
	function wl(e, t) {
		try {
			var n = t.updateQueue, r = n === null ? null : n.lastEffect;
			if (r !== null) {
				var i = r.next;
				n = i;
				do {
					if ((n.tag & e) === e) {
						r = void 0;
						var a = n.create, o = n.inst;
						r = a(), o.destroy = r;
					}
					n = n.next;
				} while (n !== i);
			}
		} catch (e) {
			Z(t, t.return, e);
		}
	}
	function Tl(e, t, n) {
		try {
			var r = t.updateQueue, i = r === null ? null : r.lastEffect;
			if (i !== null) {
				var a = i.next;
				r = a;
				do {
					if ((r.tag & e) === e) {
						var o = r.inst, s = o.destroy;
						if (s !== void 0) {
							o.destroy = void 0, i = t;
							var c = n, l = s;
							try {
								l();
							} catch (e) {
								Z(i, c, e);
							}
						}
					}
					r = r.next;
				} while (r !== a);
			}
		} catch (e) {
			Z(t, t.return, e);
		}
	}
	function El(e) {
		var t = e.updateQueue;
		if (t !== null) {
			var n = e.stateNode;
			try {
				Ao(t, n);
			} catch (t) {
				Z(e, e.return, t);
			}
		}
	}
	function Dl(e, t, n) {
		n.props = Tc(e.type, e.memoizedProps), n.state = e.memoizedState;
		try {
			n.componentWillUnmount();
		} catch (n) {
			Z(e, t, n);
		}
	}
	function Ol(e, t) {
		try {
			var n = e.ref;
			if (n !== null) {
				switch (e.tag) {
					case 26:
					case 27:
					case 5:
						var r = e.stateNode;
						break;
					case 30:
						var i = e.stateNode, a = Ci(e.memoizedProps, i);
						(i.ref === null || i.ref.name !== a) && (i.ref = Pp(a)), r = i.ref;
						break;
					case 7:
						if (e.stateNode === null) {
							var o = new Fp(e);
							h(e.child, !1, Qp, o, void 0, void 0), e.stateNode = o;
						}
						r = e.stateNode;
						break;
					default: r = e.stateNode;
				}
				typeof n == "function" ? e.refCleanup = n(r) : n.current = r;
			}
		} catch (n) {
			Z(e, t, n);
		}
	}
	function kl(e, t) {
		var n = e.ref, r = e.refCleanup;
		if (n !== null) {
			if (typeof r == "function") try {
				r();
			} catch (n) {
				Z(e, t, n);
			} finally {
				e.refCleanup = null, e = e.alternate, e != null && (e.refCleanup = null);
			}
			else if (typeof n == "function") try {
				n(null);
			} catch (n) {
				Z(e, t, n);
			}
			else n.current = null;
		}
	}
	function Al(e, t) {
		if ((e.tag === 5 || e.tag === 27 || e.tag === 6) && e.alternate === null && t !== null) for (var n = 0; n < t.length; n++) em(e.stateNode, t[n]);
	}
	function jl(e) {
		for (var t = e.return; t !== null && (Pl(t) && em(e.stateNode, t.stateNode), !Nl(t));) t = t.return;
	}
	function Ml(e) {
		for (var t = e.return; t !== null && (Pl(t) && tm(e.stateNode, t.stateNode), !Nl(t));) t = t.return;
	}
	function Nl(e) {
		return e.tag === 5 || e.tag === 3 || e.tag === 27;
	}
	function Pl(e) {
		return e && e.tag === 7 && e.stateNode !== null;
	}
	function Fl(e) {
		var t = e.type, n = e.memoizedProps, r = e.stateNode;
		try {
			a: switch (t) {
				case "button":
				case "input":
				case "select":
				case "textarea":
					n.autoFocus && r.focus();
					break a;
				case "img": n.src ? r.src = n.src : n.srcSet && (r.srcset = n.srcSet);
			}
		} catch (t) {
			Z(e, e.return, t);
		}
	}
	function Il(e, t, n) {
		try {
			var r = e.stateNode;
			ip(r, e.type, n, t), r[kt] = t;
		} catch (t) {
			Z(e, e.return, t);
		}
	}
	function Ll(e) {
		return e.tag === 5 || e.tag === 3 || e.tag === 26 || e.tag === 27 && Sp(e.type) || e.tag === 4;
	}
	function Rl(e) {
		a: for (;;) {
			for (; e.sibling === null;) {
				if (e.return === null || Ll(e.return)) return null;
				e = e.return;
			}
			for (e.sibling.return = e.return, e = e.sibling; e.tag !== 5 && e.tag !== 6 && e.tag !== 18;) {
				if (e.tag === 27 && Sp(e.type) || e.flags & 2 || e.child === null || e.tag === 4) continue a;
				e.child.return = e, e = e.child;
			}
			if (!(e.flags & 2)) return e.stateNode;
		}
	}
	function zl(e, t, n, r) {
		var i = e.tag;
		if (i === 5 || i === 6) i = e.stateNode, t ? (n.nodeType === 9 ? n.body : n.nodeName === "HTML" ? n.ownerDocument.body : n).insertBefore(i, t) : (t = n.nodeType === 9 ? n.body : n.nodeName === "HTML" ? n.ownerDocument.body : n, t.appendChild(i), n = n._reactRootContainer, n != null || t.onclick !== null || (t.onclick = wn)), Al(e, r), N = !0;
		else if (i !== 4 && (i === 27 && (Al(e, r), r = null, Sp(e.type) && (n = e.stateNode, t = null)), e = e.child, e !== null)) for (zl(e, t, n, r), e = e.sibling; e !== null;) zl(e, t, n, r), e = e.sibling;
	}
	function Bl(e, t, n, r) {
		var i = e.tag;
		if (i === 5 || i === 6) i = e.stateNode, t ? n.insertBefore(i, t) : n.appendChild(i), Al(e, r), N = !0;
		else if (i !== 4 && (i === 27 && (Al(e, r), r = null, Sp(e.type) && (n = e.stateNode)), e = e.child, e !== null)) for (Bl(e, t, n, r), e = e.sibling; e !== null;) Bl(e, t, n, r), e = e.sibling;
	}
	function Vl(e) {
		var t = e.stateNode, n = e.memoizedProps;
		try {
			for (var r = e.type, i = t.attributes; i.length;) t.removeAttributeNode(i[0]);
			np(t, r, n), t[Ot] = e, t[kt] = n;
		} catch (t) {
			Z(e, e.return, t);
		}
	}
	var Hl = !1, Ul = null;
	function Wl(e) {
		(e.tag === 30 || e.subtreeFlags & 33554432) && (Hl = !0);
	}
	var Gl = null;
	function Kl() {
		var e = Gl;
		return Gl = null, e;
	}
	var ql = 0;
	function Jl(e, t, n, r, i) {
		return ql = 0, Yl(e.child, t, n, r, i);
	}
	function Yl(e, t, n, r, i) {
		for (var a = !1; e !== null;) {
			if (e.tag === 5) {
				var o = e.stateNode;
				if (r !== null) {
					var s = Op(o);
					r.push(s), s.view && (a = !0);
				} else a || Op(o).view && (a = !0);
				Hl = !0, Tp(o, ql === 0 ? t : t + "_" + ql, n), ql++;
			} else e.tag === 22 && e.memoizedState !== null || e.tag === 30 && i || Yl(e.child, t, n, r, i) && (a = !0);
			e = e.sibling;
		}
		return a;
	}
	function Xl(e, t) {
		for (; e !== null;) e.tag === 5 ? Ep(e.stateNode, e.memoizedProps) : e.tag === 22 && e.memoizedState !== null || e.tag === 30 && t || Xl(e.child, t), e = e.sibling;
	}
	function Zl(e) {
		if (e.subtreeFlags & 18874368) for (e = e.child; e !== null;) {
			if ((e.tag !== 22 || e.memoizedState === null) && (Zl(e), e.tag === 30 && e.flags & 18874368 && e.stateNode.paired)) {
				var t = e.memoizedProps;
				if (t.name == null || t.name === "auto") throw Error(i(544));
				var n = t.name;
				t = Ti(t.default, t.share), t !== "none" && (Jl(e, n, t, null, !1) || Xl(e.child, !1));
			}
			e = e.sibling;
		}
	}
	function Ql(e, t) {
		if (e.tag === 30) {
			var n = e.stateNode, r = e.memoizedProps, i = Ci(r, n), a = Ti(r.default, n.paired ? r.share : r.enter);
			a === "none" ? Zl(e) : Jl(e, i, a, null, !1) ? (Zl(e), n.paired || t || Nd(e, r.onEnter)) : Xl(e.child, !1);
		} else if (e.subtreeFlags & 33554432) for (e = e.child; e !== null;) Ql(e, t), e = e.sibling;
		else Zl(e);
	}
	function $l(e) {
		if (Ul !== null && Ul.size !== 0) {
			var t = Ul;
			if (e.subtreeFlags & 18874368) for (e = e.child; e !== null;) {
				if (e.tag !== 22 || e.memoizedState === null) {
					if (e.tag === 30 && e.flags & 18874368) {
						var n = e.memoizedProps, r = n.name;
						if (r != null && r !== "auto") {
							var i = t.get(r);
							if (i !== void 0) {
								var a = Ti(n.default, n.share);
								if (a !== "none" && (Jl(e, r, a, null, !1) ? (a = e.stateNode, i.paired = a, a.paired = i, Nd(e, n.onShare)) : Xl(e.child, !1)), t.delete(r), t.size === 0) break;
							}
						}
					}
					$l(e);
				}
				e = e.sibling;
			}
		}
	}
	function eu(e) {
		if (e.tag === 30) {
			var t = e.memoizedProps, n = Ci(t, e.stateNode), r = Ul === null ? void 0 : Ul.get(n), i = Ti(t.default, r === void 0 ? t.exit : t.share);
			i !== "none" && (Jl(e, n, i, null, !1) ? r === void 0 ? Nd(e, t.onExit) : (i = e.stateNode, r.paired = i, i.paired = r, Ul.delete(n), Nd(e, t.onShare)) : Xl(e.child, !1)), Ul !== null && $l(e);
		} else if (e.subtreeFlags & 33554432) for (e = e.child; e !== null;) eu(e), e = e.sibling;
		else Ul !== null && $l(e);
	}
	function tu(e) {
		for (e = e.child; e !== null;) {
			if (e.tag === 30) {
				var t = e.memoizedProps, n = Ci(t, e.stateNode);
				t = Ti(t.default, t.update), e.flags &= -5, t !== "none" && Jl(e, n, t, e.memoizedState = [], !1);
			} else e.subtreeFlags & 33554432 && tu(e);
			e = e.sibling;
		}
	}
	function nu(e) {
		if (e.subtreeFlags & 18874368) for (e = e.child; e !== null;) {
			if (e.tag !== 22 || e.memoizedState === null) {
				if (e.tag === 30 && e.flags & 18874368) {
					var t = e.stateNode;
					t.paired !== null && (t.paired = null, Xl(e.child, !1));
				}
				nu(e);
			}
			e = e.sibling;
		}
	}
	function ru(e) {
		if (e.tag === 30) e.stateNode.paired = null, Xl(e.child, !1), nu(e);
		else if (e.subtreeFlags & 33554432) for (e = e.child; e !== null;) ru(e), e = e.sibling;
		else nu(e);
	}
	function iu(e) {
		for (e = e.child; e !== null;) e.tag === 30 ? Xl(e.child, !1) : e.subtreeFlags & 33554432 && iu(e), e = e.sibling;
	}
	function au(e, t, n, r, i, a, o) {
		for (var s = !1; t !== null;) {
			if (t.tag === 5) {
				var c = t.stateNode;
				if (a !== null && ql < a.length) {
					var l = a[ql], u = Op(c);
					(l.view || u.view) && (s = !0);
					var d;
					if (d = !(e.flags & 4)) {
						if (u.clip) d = !0;
						else {
							d = l.rect;
							var f = u.rect;
							d = d.y !== f.y || d.x !== f.x || d.height !== f.height || d.width !== f.width;
						}
					}
					d && (e.flags |= 4), u.abs ? u = !l.abs : (l = l.rect, u = u.rect, u = l.height !== u.height || l.width !== u.width), u && (e.flags |= 32);
				} else e.flags |= 32;
				e.flags & 4 && Tp(c, ql === 0 ? n : n + "_" + ql, i), s && e.flags & 4 || (Gl === null && (Gl = []), Gl.push(c, ql === 0 ? r : r + "_" + ql, t.memoizedProps)), ql++;
			} else (t.tag !== 22 || t.memoizedState === null) && (t.tag === 30 && o ? e.flags |= t.flags & 32 : au(e, t.child, n, r, i, a, o) && (s = !0));
			t = t.sibling;
		}
		return s;
	}
	function ou(e, t) {
		for (e = e.child; e !== null;) {
			if (e.tag === 30) {
				var n = e.memoizedProps, r = e.stateNode, i = Ci(n, r), a = Ti(n.default, n.update);
				if (t) {
					r = r.clones;
					var o = r === null ? null : r.map(kp);
				} else o = e.memoizedState, e.memoizedState = null;
				r = e;
				var s = e.child;
				ql = 0, i = au(r, s, i, i, a, o, !1), e.flags & 4 && i && (t || Nd(e, n.onUpdate));
			} else e.subtreeFlags & 33554432 && ou(e, t);
			e = e.sibling;
		}
	}
	var su = !1, H = !1, cu = !1, lu = !1, uu = typeof WeakSet == "function" ? WeakSet : Set, du = null, fu = !1, pu = !1, mu = !1, hu = !1;
	function gu(e, t, n) {
		if (e = e.containerInfo, sp = gh, e = $r(e), ei(e)) {
			if ("selectionStart" in e) var r = {
				start: e.selectionStart,
				end: e.selectionEnd
			};
			else a: {
				r = (r = e.ownerDocument) && r.defaultView || window;
				var i = r.getSelection && r.getSelection();
				if (i && i.rangeCount !== 0) {
					r = i.anchorNode;
					var a = i.anchorOffset, o = i.focusNode;
					i = i.focusOffset;
					try {
						r.nodeType, o.nodeType;
					} catch {
						r = null;
						break a;
					}
					var s = 0, c = -1, l = -1, u = 0, d = 0, f = e, p = null;
					b: for (;;) {
						for (var m; f !== r || a !== 0 && f.nodeType !== 3 || (c = s + a), f !== o || i !== 0 && f.nodeType !== 3 || (l = s + i), f.nodeType === 3 && (s += f.nodeValue.length), (m = f.firstChild) !== null;) p = f, f = m;
						for (;;) {
							if (f === e) break b;
							if (p === r && ++u === a && (c = s), p === o && ++d === i && (l = s), (m = f.nextSibling) !== null) break;
							f = p, p = f.parentNode;
						}
						f = m;
					}
					r = c === -1 || l === -1 ? null : {
						start: c,
						end: l
					};
				} else r = null;
			}
			r ||= {
				start: 0,
				end: 0
			};
		} else r = null;
		for (cp = {
			focusedElem: e,
			selectionRange: r
		}, gh = !1, n = (n & 335544064) === n, du = t, t = n ? 9270 : 1024; du !== null;) {
			if (e = du, n && (r = e.deletions, r !== null)) for (a = 0; a < r.length; a++) n && eu(r[a]);
			if (e.alternate === null && e.flags & 2) n && Wl(e), _u(n);
			else {
				if (e.tag === 22) {
					if (r = e.alternate, e.memoizedState !== null) {
						r !== null && r.memoizedState === null && n && eu(r), _u(n);
						continue;
					}
					if (r !== null && r.memoizedState !== null) {
						n && Wl(e), _u(n);
						continue;
					}
				}
				r = e.child, (e.subtreeFlags & t) !== 0 && r !== null ? (r.return = e, du = r) : (n && tu(e), _u(n));
			}
		}
		Ul = null;
	}
	function _u(e) {
		for (; du !== null;) {
			var t = du, n = e, r = t.alternate, a = t.flags;
			switch (t.tag) {
				case 0:
				case 11:
				case 15: break;
				case 1:
					if (a & 1024 && r !== null) {
						n = void 0, a = r.memoizedProps, r = r.memoizedState;
						var o = t.stateNode;
						try {
							var s = Tc(t.type, a);
							n = o.getSnapshotBeforeUpdate(s, r), o.__reactInternalSnapshotBeforeUpdate = n;
						} catch (e) {
							Z(t, t.return, e);
						}
					}
					break;
				case 3:
					if (a & 1024) {
						if (r = t.stateNode.containerInfo, n = r.nodeType, n === 9) nm(r);
						else if (n === 1) switch (r.nodeName) {
							case "HEAD":
							case "HTML":
							case "BODY":
								nm(r);
								break;
							default: r.textContent = "";
						}
					}
					break;
				case 5:
				case 26:
				case 27:
				case 6:
				case 4:
				case 17: break;
				case 30:
					n && r !== null && (n = Ci(r.memoizedProps, r.stateNode), a = t.memoizedProps, a = Ti(a.default, a.update), a !== "none" && Jl(r, n, a, r.memoizedState = [], !0));
					break;
				default: if (a & 1024) throw Error(i(163));
			}
			if (r = t.sibling, r !== null) {
				r.return = t.return, du = r;
				break;
			}
			du = t.return;
		}
	}
	function vu(e, t, n) {
		var r = n.flags;
		switch (n.tag) {
			case 0:
			case 11:
			case 15:
				Lu(e, n), r & 4 && wl(5, n);
				break;
			case 1:
				if (Lu(e, n), r & 4) {
					if (e = n.stateNode, t === null) try {
						e.componentDidMount();
					} catch (e) {
						Z(n, n.return, e);
					}
					else {
						var i = Tc(n.type, t.memoizedProps);
						t = t.memoizedState;
						try {
							e.componentDidUpdate(i, t, e.__reactInternalSnapshotBeforeUpdate);
						} catch (e) {
							Z(n, n.return, e);
						}
					}
				}
				r & 64 && El(n), r & 512 && Ol(n, n.return);
				break;
			case 3:
				if (Lu(e, n), r & 64 && (e = n.updateQueue, e !== null)) {
					if (t = null, n.child !== null) switch (n.child.tag) {
						case 27:
						case 5:
							t = n.child.stateNode;
							break;
						case 1: t = n.child.stateNode;
					}
					try {
						Ao(e, t);
					} catch (e) {
						Z(n, n.return, e);
					}
				}
				break;
			case 27: t === null && r & 4 && Vl(n);
			case 26:
			case 5:
				Lu(e, n), t === null && r & 4 && Fl(n), r & 512 && Ol(n, n.return);
				break;
			case 12:
				Lu(e, n);
				break;
			case 31:
				Lu(e, n), r & 4 && Eu(e, n);
				break;
			case 13:
				Lu(e, n), r & 4 && Du(e, n), r & 64 && (e = n.memoizedState, e !== null && (e = e.dehydrated, e !== null && (n = _f.bind(null, n), cm(e, n))));
				break;
			case 22:
				if (r = n.memoizedState !== null || su, !r) {
					var a = t !== null && t.memoizedState !== null || H;
					t = su, i = H, su = r, (H = a) && !i ? (r = 2, n.subtreeFlags & 8772 && (r |= 1), zu(e, n, r)) : Lu(e, n), su = t, H = i;
				}
				break;
			case 30:
				Lu(e, n), r & 512 && Ol(n, n.return);
				break;
			case 7: r & 512 && Ol(n, n.return);
			default: Lu(e, n);
		}
	}
	function yu(e, t) {
		for (e = e.child; e !== null;) bu(e, t), e = e.sibling;
	}
	function bu(e, t) {
		switch (e.tag) {
			case 5:
			case 26:
				try {
					var n = e.stateNode;
					if (t) {
						var r = n.style;
						typeof r.setProperty == "function" ? r.setProperty("display", "none", "important") : r.display = "none";
					} else {
						var i = e.stateNode, a = e.memoizedProps.style, o = a != null && a.hasOwnProperty("display") ? a.display : null;
						i.style.display = o == null || typeof o == "boolean" ? "" : ("" + o).trim();
					}
				} catch (t) {
					Z(e, e.return, t);
				}
				xu(e, t);
				break;
			case 6:
				try {
					e.stateNode.nodeValue = t ? "" : e.memoizedProps, N = !0;
				} catch (t) {
					Z(e, e.return, t);
				}
				break;
			case 18:
				try {
					var s = e.stateNode;
					t ? wp(s, !0) : wp(e.stateNode, !1);
				} catch (t) {
					Z(e, e.return, t);
				}
				break;
			case 22:
			case 23:
				e.memoizedState === null && yu(e, t);
				break;
			default: yu(e, t);
		}
	}
	function xu(e, t) {
		if (e.subtreeFlags & 67108864) for (e = e.child; e !== null;) {
			a: {
				var n = e, r = t;
				switch (n.tag) {
					case 4:
						bu(n, r);
						break a;
					case 22:
						n.memoizedState === null && xu(n, r);
						break a;
					default: xu(n, r);
				}
			}
			e = e.sibling;
		}
	}
	function Su(e) {
		var t = e.alternate;
		t !== null && (e.alternate = null, Su(t)), e.child = null, e.deletions = null, e.sibling = null, e.tag === 5 && (t = e.stateNode, t !== null && Lt(t)), e.stateNode = null, e.return = null, e.dependencies = null, e.memoizedProps = null, e.memoizedState = null, e.pendingProps = null, e.stateNode = null, e.updateQueue = null;
	}
	var U = null, Cu = !1;
	function wu(e, t, n) {
		for (n = n.child; n !== null;) Tu(e, t, n), n = n.sibling;
	}
	function Tu(e, t, n) {
		if (rt && typeof rt.onCommitFiberUnmount == "function") try {
			rt.onCommitFiberUnmount(nt, n);
		} catch {}
		switch (n.tag) {
			case 26:
				H || kl(n, t), wu(e, t, n), n.memoizedState ? n.memoizedState.count-- : n.stateNode && !H && (n = n.stateNode, n.parentNode.removeChild(n));
				break;
			case 27:
				H || kl(n, t), Ml(n);
				var r = U, i = Cu;
				Sp(n.type) && (U = n.stateNode, Cu = !1), wu(e, t, n), gm(n.stateNode, n.type, n.memoizedProps), U = r, Cu = i;
				break;
			case 5: H || kl(n, t), Ml(n);
			case 6:
				if (n.tag === 6 && Ml(n), r = U, i = Cu, U = null, wu(e, t, n), U = r, Cu = i, U !== null) {
					if (Cu) try {
						(U.nodeType === 9 ? U.body : U.nodeName === "HTML" ? U.ownerDocument.body : U).removeChild(n.stateNode), N = !0;
					} catch (e) {
						Z(n, t, e);
					}
					else try {
						U.removeChild(n.stateNode), N = !0;
					} catch (e) {
						Z(n, t, e);
					}
				}
				break;
			case 18:
				U !== null && (Cu ? (e = U, Cp(e.nodeType === 9 ? e.body : e.nodeName === "HTML" ? e.ownerDocument.body : e, n.stateNode), Hh(e)) : Cp(U, n.stateNode));
				break;
			case 4:
				r = U, i = Cu, U = n.stateNode.containerInfo, Cu = !0, wu(e, t, n), U = r, Cu = i;
				break;
			case 0:
			case 11:
			case 14:
			case 15:
				Tl(2, n, t), H || Tl(4, n, t), wu(e, t, n);
				break;
			case 1:
				H || (kl(n, t), r = n.stateNode, typeof r.componentWillUnmount == "function" && Dl(n, t, r)), wu(e, t, n);
				break;
			case 21:
				wu(e, t, n);
				break;
			case 22:
				H = (r = H) || n.memoizedState !== null, wu(e, t, n), H = r;
				break;
			case 30:
				kl(n, t), wu(e, t, n);
				break;
			case 7:
				H || kl(n, t), wu(e, t, n);
				break;
			default: wu(e, t, n);
		}
	}
	function Eu(e, t) {
		if (t.memoizedState === null && (e = t.alternate, e !== null && (e = e.memoizedState, e !== null))) {
			e = e.dehydrated;
			try {
				Hh(e);
			} catch (e) {
				Z(t, t.return, e);
			}
		}
	}
	function Du(e, t) {
		if (t.memoizedState === null && (e = t.alternate, e !== null && (e = e.memoizedState, e !== null && (e = e.dehydrated, e !== null)))) try {
			Hh(e);
		} catch (e) {
			Z(t, t.return, e);
		}
	}
	function Ou(e) {
		switch (e.tag) {
			case 31:
			case 13:
			case 19:
				var t = e.stateNode;
				return t === null && (t = e.stateNode = new uu()), t;
			case 22: return e = e.stateNode, t = e._retryCache, t === null && (t = e._retryCache = new uu()), t;
			default: throw Error(i(435, e.tag));
		}
	}
	function ku(e, t) {
		var n = Ou(e);
		t.forEach(function(t) {
			if (!n.has(t)) {
				n.add(t);
				var r = vf.bind(null, e, t);
				t.then(r, r);
			}
		});
	}
	function Au(e, t, n) {
		var r = t.deletions;
		if (r !== null) for (var a = 0; a < r.length; a++) {
			var o = r[a], s = e, c = t, l = c;
			a: for (; l !== null;) {
				switch (l.tag) {
					case 27:
						if (Sp(l.type)) {
							U = l.stateNode, Cu = !1;
							break a;
						}
						break;
					case 5:
						U = l.stateNode, Cu = !1;
						break a;
					case 3:
					case 4:
						U = l.stateNode.containerInfo, Cu = !0;
						break a;
				}
				l = l.return;
			}
			if (U === null) throw Error(i(160));
			Tu(s, c, o), U = null, Cu = !1, s = o.alternate, s !== null && (s.return = null), o.return = null;
		}
		if (t.subtreeFlags & 13886) for (t = t.child; t !== null;) Mu(t, e, n), t = t.sibling;
	}
	var ju = null;
	function Mu(e, t, n) {
		var r = e.alternate, a = e.flags;
		switch (e.tag) {
			case 0:
			case 11:
			case 14:
			case 15:
				if (a & 4 && (r = e.updateQueue, r = r === null ? null : r.events, r !== null)) for (var o = 0; o < r.length; o++) {
					var s = r[o];
					s.ref.impl = s.nextImpl;
				}
				Au(t, e, n), Nu(e), a & 4 && (Tl(3, e, e.return), wl(3, e), Tl(5, e, e.return));
				break;
			case 1:
				Au(t, e, n), Nu(e), a & 512 && (H || r === null || kl(r, r.return)), a & 64 && su && (e = e.updateQueue, e !== null && (t = e.callbacks, t !== null && (n = e.shared.hiddenCallbacks, e.shared.hiddenCallbacks = n === null ? t : n.concat(t))));
				break;
			case 26:
				if (o = ju, Au(t, e, n), Nu(e), a & 512 && (H || r === null || kl(r, r.return)), a & 4) {
					if (a = r === null ? null : r.memoizedState, n = e.memoizedState, r === null) {
						if (n === null) {
							if (e.stateNode === null) {
								if (su) e.stateNode = fp(e.type, e.memoizedProps, t.containerInfo, e);
								else {
									a: {
										t = e.type, n = e.memoizedProps, a = o.ownerDocument || o;
										b: switch (t) {
											case "title":
												r = a.getElementsByTagName("title")[0], (!r || r[Ft] || r[Ot] || r.namespaceURI === "http://www.w3.org/2000/svg" || r.hasAttribute("itemprop")) && (r = a.createElement(t), a.head.insertBefore(r, a.querySelector("head > title"))), np(r, t, n), r[Ot] = e, Ht(r), t = r;
												break a;
											case "link":
												if (o = Gm("link", "href", a).get(t + (n.href || ""))) {
													for (s = 0; s < o.length; s++) if (r = o[s], r.getAttribute("href") === (n.href == null || n.href === "" ? null : n.href) && r.getAttribute("rel") === (n.rel == null ? null : n.rel) && r.getAttribute("title") === (n.title == null ? null : n.title) && r.getAttribute("crossorigin") === (n.crossOrigin == null ? null : n.crossOrigin)) {
														o.splice(s, 1);
														break b;
													}
												}
												r = a.createElement(t), np(r, t, n), a.head.appendChild(r);
												break;
											case "meta":
												if (o = Gm("meta", "content", a).get(t + (n.content || ""))) {
													for (s = 0; s < o.length; s++) if (r = o[s], r.getAttribute("content") === (n.content == null ? null : "" + n.content) && r.getAttribute("name") === (n.name == null ? null : n.name) && r.getAttribute("property") === (n.property == null ? null : n.property) && r.getAttribute("http-equiv") === (n.httpEquiv == null ? null : n.httpEquiv) && r.getAttribute("charset") === (n.charSet == null ? null : n.charSet)) {
														o.splice(s, 1);
														break b;
													}
												}
												r = a.createElement(t), np(r, t, n), a.head.appendChild(r);
												break;
											default: throw Error(i(468, t));
										}
										r[Ot] = e, Ht(r), t = r;
									}
									e.stateNode = t;
								}
							} else su || Km(o, e.type, e.stateNode);
						} else e.stateNode = Bm(o, n, e.memoizedProps);
					} else a === n ? n === null && e.stateNode !== null && Il(e, e.memoizedProps, r.memoizedProps) : (a === null ? (t = r.stateNode, t === null || H || t.parentNode.removeChild(t)) : a.count--, n === null ? su || Km(o, e.type, e.stateNode) : Bm(o, n, e.memoizedProps));
				}
				break;
			case 27:
				Au(t, e, n), Nu(e), a & 512 && (H || r === null || kl(r, r.return)), r !== null && a & 4 && Il(e, e.memoizedProps, r.memoizedProps);
				break;
			case 5:
				if (o = cu, cu = !1, Au(t, e, n), cu = o, Nu(e), a & 512 && (H || r === null || kl(r, r.return)), e.flags & 32) {
					t = e.stateNode;
					try {
						gn(t, ""), N = !0;
					} catch (t) {
						Z(e, e.return, t);
					}
				}
				a & 4 && e.stateNode != null && (t = e.memoizedProps, Il(e, t, r === null ? t : r.memoizedProps)), a & 1024 && (lu = !0);
				break;
			case 6:
				if (Au(t, e, n), Nu(e), a & 4) {
					if (e.stateNode === null) throw Error(i(162));
					t = e.memoizedProps, n = e.stateNode;
					try {
						n.nodeValue = t, N = !0;
					} catch (t) {
						Z(e, e.return, t);
					}
				}
				break;
			case 3:
				if (N = !1, Wm = null, o = ju, ju = bm(t.containerInfo), Au(t, e, n), ju = o, Nu(e), a & 4 && r !== null && r.memoizedState.isDehydrated) try {
					Hh(t.containerInfo);
				} catch (t) {
					Z(e, e.return, t);
				}
				lu && (lu = !1, Pu(e)), N = !1;
				break;
			case 4:
				a = cu, cu = su, r = Qt(), o = ju, ju = bm(e.stateNode.containerInfo), Au(t, e, n), Nu(e), ju = o, N && pu && (mu = !0), N = r, cu = a;
				break;
			case 12:
				Au(t, e, n), Nu(e);
				break;
			case 31:
				Au(t, e, n), Nu(e), a & 4 && (t = e.updateQueue, t !== null && (e.updateQueue = null, ku(e, t)));
				break;
			case 13:
				Au(t, e, n), Nu(e), e.child.flags & 8192 && e.memoizedState !== null != (r !== null && r.memoizedState !== null) && (hd = qe()), a & 4 && (t = e.updateQueue, t !== null && (e.updateQueue = null, ku(e, t)));
				break;
			case 22:
				o = e.memoizedState !== null, s = r !== null && r.memoizedState !== null;
				var c = su, l = H, u = cu;
				su = c || o, cu = u || o, H = l || s, Au(t, e, n), H = l, cu = u, su = c, Nu(e), a & 8192 && (t = e.stateNode, t._visibility = o ? t._visibility & -2 : t._visibility | 1, !o || r === null || s || su || H || (t = s || H, n = su, r = H, su = o || su, H = t, Ru(e, 2), su = n, H = r), !o && cu || yu(e, o)), a & 4 && (t = e.updateQueue, t !== null && (n = t.retryQueue, n !== null && (t.retryQueue = null, ku(e, n))));
				break;
			case 19:
				Au(t, e, n), Nu(e), a & 4 && (t = e.updateQueue, t !== null && (e.updateQueue = null, ku(e, t)));
				break;
			case 30:
				a & 512 && (H || r === null || kl(r, r.return)), a = Qt(), o = pu, s = (n & 335544064) === n, c = e.memoizedProps, pu = s && Ti(c.default, c.update) !== "none", Au(t, e, n), Nu(e), s && r !== null && N && (e.flags |= 4), pu = o, N = a;
				break;
			case 21: break;
			case 7: a & 512 && (H || r === null || kl(r, r.return)), r && r.stateNode !== null && (r.stateNode._fragmentFiber = e);
			default: Au(t, e, n), Nu(e);
		}
	}
	function Nu(e) {
		var t = e.flags;
		if (t & 2) {
			try {
				for (var n, r = e.return; r !== null;) {
					if (Ll(r)) {
						n = r;
						break;
					}
					r = r.return;
				}
				r = null;
				for (var a = e.return; a !== null;) {
					if (Pl(a)) {
						var o = a.stateNode;
						r === null ? r = [o] : r.push(o);
					}
					if (Nl(a)) break;
					a = a.return;
				}
				var s = r;
				if (n == null) throw Error(i(160));
				switch (n.tag) {
					case 27:
						var c = n.stateNode;
						Bl(e, Rl(e), c, s);
						break;
					case 5:
						var l = n.stateNode;
						n.flags & 32 && (gn(l, ""), n.flags &= -33), Bl(e, Rl(e), l, s);
						break;
					case 3:
					case 4:
						var u = n.stateNode.containerInfo;
						zl(e, Rl(e), u, s);
						break;
					default: throw Error(i(161));
				}
			} catch (t) {
				Z(e, e.return, t);
			}
			e.flags &= -3;
		}
		t & 4096 && (e.flags &= -4097);
	}
	function Pu(e) {
		if (e.subtreeFlags & 1024) for (e = e.child; e !== null;) {
			var t = e;
			Pu(t), t.tag === 5 && t.flags & 1024 && (t = t.stateNode, gh = !0, t.reset(), gh = !1), e = e.sibling;
		}
	}
	function Fu(e, t) {
		if (t.subtreeFlags & 9270) for (t = t.child; t !== null;) Iu(t, e), t = t.sibling;
		else ou(t, !1);
	}
	function Iu(e, t) {
		var n = e.alternate;
		if (n === null) Ql(e, !1);
		else switch (e.tag) {
			case 3:
				if (hu = fu = !1, Kl(), Fu(t, e), !fu && !mu) {
					if (e = Gl, e !== null) for (var r = 0; r < e.length; r += 3) {
						n = e[r];
						var i = e[r + 1];
						Ep(n, e[r + 2]), n = n.ownerDocument.documentElement, n !== null && n.animate({
							opacity: [0, 0],
							pointerEvents: ["none", "none"]
						}, {
							duration: 0,
							fill: "forwards",
							pseudoElement: "::view-transition-group(" + i + ")"
						});
					}
					e = t.containerInfo, e = e.nodeType === 9 ? e.documentElement : e.ownerDocument.documentElement, e !== null && e.style.viewTransitionName === "" && (e.style.viewTransitionName = "none", e.animate({
						opacity: [0, 0],
						pointerEvents: ["none", "none"]
					}, {
						duration: 0,
						fill: "forwards",
						pseudoElement: "::view-transition-group(root)"
					}), e.animate({
						width: [0, 0],
						height: [0, 0]
					}, {
						duration: 0,
						fill: "forwards",
						pseudoElement: "::view-transition"
					})), hu = !0;
				}
				Gl = null;
				break;
			case 5:
				Fu(t, e);
				break;
			case 4:
				r = fu, fu = !1, Fu(t, e), fu && (mu = !0), fu = r;
				break;
			case 22:
				e.memoizedState === null && (n.memoizedState === null ? Fu(t, e) : Ql(e, !1));
				break;
			case 30:
				r = fu, i = Kl(), fu = !1, Fu(t, e), fu && (e.flags |= 4);
				var a = e.memoizedProps, o = e.stateNode;
				t = Ci(a, o), o = Ci(n.memoizedProps, o);
				var s = Ti(a.default, a.update);
				s === "none" ? t = !1 : (a = n.memoizedState, n.memoizedState = null, n = e.child, ql = 0, t = au(e, n, t, o, s, a, !0), ql !== (a === null ? 0 : a.length) && (e.flags |= 32)), e.flags & 4 && t ? (Nd(e, e.memoizedProps.onUpdate), Gl = i) : i !== null && (i.push.apply(i, Gl), Gl = i), fu = e.flags & 32 ? !0 : r;
				break;
			default: Fu(t, e);
		}
	}
	function Lu(e, t) {
		if (t.subtreeFlags & 8772) for (t = t.child; t !== null;) vu(e, t.alternate, t), t = t.sibling;
	}
	function Ru(e, t) {
		for (e = e.child; e !== null;) {
			var n = e, r = t;
			switch (n.tag) {
				case 0:
				case 11:
				case 14:
				case 15:
					Tl(4, n, n.return), Ru(n, r);
					break;
				case 1:
					kl(n, n.return);
					var i = n.stateNode;
					typeof i.componentWillUnmount == "function" && Dl(n, n.return, i), Ru(n, r);
					break;
				case 27: r & 2 && gm(n.stateNode, n.type, n.memoizedProps);
				case 5:
					kl(n, n.return), n.tag !== 5 && n.tag !== 27 || Ml(n), Ru(n, r);
					break;
				case 6:
					Ml(n);
					break;
				case 26:
					kl(n, n.return), i = n.stateNode, n.memoizedState !== null || i === null || H || i.parentNode.removeChild(i), Ru(n, r);
					break;
				case 22:
					n.memoizedState === null && Ru(n, r);
					break;
				case 30:
					kl(n, n.return), Ru(n, r);
					break;
				case 7: kl(n, n.return);
				default: Ru(n, r);
			}
			e = e.sibling;
		}
	}
	function zu(e, t, n) {
		for (n = t.subtreeFlags & 8772 ? n : n & -2, t = t.child; t !== null;) {
			var r = t.alternate, i = e, a = t, o = a.flags, s = !!(n & 1);
			switch (a.tag) {
				case 0:
				case 11:
				case 15:
					zu(i, a, n), wl(4, a);
					break;
				case 1:
					if (zu(i, a, n), r = a, i = r.stateNode, typeof i.componentDidMount == "function") try {
						i.componentDidMount();
					} catch (e) {
						Z(r, r.return, e);
					}
					if (r = a, i = r.updateQueue, i !== null) {
						var c = r.stateNode;
						try {
							var l = i.shared.hiddenCallbacks;
							if (l !== null) for (i.shared.hiddenCallbacks = null, i = 0; i < l.length; i++) ko(l[i], c);
						} catch (e) {
							Z(r, r.return, e);
						}
					}
					s && o & 64 && El(a), Ol(a, a.return);
					break;
				case 27: n & 2 && Vl(a);
				case 5:
					a.tag !== 5 && a.tag !== 27 || jl(a), zu(i, a, n), s && r === null && o & 4 && Fl(a), Ol(a, a.return);
					break;
				case 6:
					jl(a);
					break;
				case 26:
					c = a.stateNode, a.memoizedState !== null || c === null || su || Km(bm(c.ownerDocument), a.type, c), zu(i, a, n), s && r === null && o & 4 && Fl(a), Ol(a, a.return);
					break;
				case 12:
					zu(i, a, n);
					break;
				case 31:
					zu(i, a, n), s && o & 4 && Eu(i, a);
					break;
				case 13:
					zu(i, a, n), s && o & 4 && Du(i, a);
					break;
				case 22:
					a.memoizedState === null && zu(i, a, n), Ol(a, a.return);
					break;
				case 30:
					zu(i, a, n), Ol(a, a.return);
					break;
				case 7: Ol(a, a.return);
				default: zu(i, a, n);
			}
			t = t.sibling;
		}
	}
	function Bu(e, t) {
		var n = null;
		e !== null && e.memoizedState !== null && e.memoizedState.cachePool !== null && (n = e.memoizedState.cachePool.pool), e = null, t.memoizedState !== null && t.memoizedState.cachePool !== null && (e = t.memoizedState.cachePool.pool), e !== n && (e != null && e.refCount++, n != null && Ra(n));
	}
	function Vu(e, t) {
		e = null, t.alternate !== null && (e = t.alternate.memoizedState.cache), t = t.memoizedState.cache, t !== e && (t.refCount++, e != null && Ra(e));
	}
	function Hu(e, t, n, r) {
		var i = (n & 335544064) === n;
		if (t.subtreeFlags & (i ? 10262 : 10256)) for (t = t.child; t !== null;) Uu(e, t, n, r), t = t.sibling;
		else i && iu(t);
	}
	function Uu(e, t, n, r) {
		var i = (n & 335544064) === n;
		i && t.alternate === null && t.return !== null && t.return.alternate !== null && ru(t);
		var a = t.flags;
		switch (t.tag) {
			case 0:
			case 11:
			case 15:
				Hu(e, t, n, r), a & 2048 && wl(9, t);
				break;
			case 1:
				Hu(e, t, n, r);
				break;
			case 3:
				Hu(e, t, n, r), i && hu && (e = e.containerInfo, e = e.nodeType === 9 ? e.body : e.nodeName === "HTML" ? e.ownerDocument.body : e, e.style.viewTransitionName === "root" && (e.style.viewTransitionName = ""), e = e.ownerDocument.documentElement, e !== null && e.style.viewTransitionName === "none" && (e.style.viewTransitionName = "")), a & 2048 && (a = null, t.alternate !== null && (a = t.alternate.memoizedState.cache), t = t.memoizedState.cache, t !== a && (t.refCount++, a != null && Ra(a)));
				break;
			case 12:
				if (a & 2048) {
					Hu(e, t, n, r), a = t.stateNode;
					try {
						var o = t.memoizedProps, s = o.id, c = o.onPostCommit;
						typeof c == "function" && c(s, t.alternate === null ? "mount" : "update", a.passiveEffectDuration, -0);
					} catch (e) {
						Z(t, t.return, e);
					}
				} else Hu(e, t, n, r);
				break;
			case 31:
				Hu(e, t, n, r);
				break;
			case 13:
				Hu(e, t, n, r);
				break;
			case 23: break;
			case 22:
				o = t.stateNode, s = t.alternate, t.memoizedState === null ? (i && s !== null && s.memoizedState !== null && ru(t), o._visibility & 2 ? Hu(e, t, n, r) : (o._visibility |= 2, Wu(e, t, n, r, !!(t.subtreeFlags & 10256) || !1))) : (i && s !== null && s.memoizedState === null && ru(s), o._visibility & 2 ? Hu(e, t, n, r) : Gu(e, t)), a & 2048 && Bu(s, t);
				break;
			case 24:
				Hu(e, t, n, r), a & 2048 && Vu(t.alternate, t);
				break;
			case 30:
				i && (a = t.alternate, a !== null && (Xl(a.child, !0), Xl(t.child, !0))), Hu(e, t, n, r);
				break;
			default: Hu(e, t, n, r);
		}
	}
	function Wu(e, t, n, r, i) {
		for (i &&= !!(t.subtreeFlags & 10256) || !1, t = t.child; t !== null;) {
			var a = e, o = t, s = n, c = r, l = o.flags;
			switch (o.tag) {
				case 0:
				case 11:
				case 15:
					Wu(a, o, s, c, i), wl(8, o);
					break;
				case 23: break;
				case 22:
					var u = o.stateNode;
					o.memoizedState === null ? (u._visibility |= 2, Wu(a, o, s, c, i)) : u._visibility & 2 ? Wu(a, o, s, c, i) : Gu(a, o), i && l & 2048 && Bu(o.alternate, o);
					break;
				case 24:
					Wu(a, o, s, c, i), i && l & 2048 && Vu(o.alternate, o);
					break;
				default: Wu(a, o, s, c, i);
			}
			t = t.sibling;
		}
	}
	function Gu(e, t) {
		if (t.subtreeFlags & 10256) for (t = t.child; t !== null;) {
			var n = e, r = t, i = r.flags;
			switch (r.tag) {
				case 22:
					Gu(n, r), i & 2048 && Bu(r.alternate, r);
					break;
				case 24:
					Gu(n, r), i & 2048 && Vu(r.alternate, r);
					break;
				default: Gu(n, r);
			}
			t = t.sibling;
		}
	}
	var Ku = 8192;
	function qu(e, t, n) {
		if (e.subtreeFlags & Ku) for (e = e.child; e !== null;) Ju(e, t, n), e = e.sibling;
	}
	function Ju(e, t, n) {
		switch (e.tag) {
			case 26:
				qu(e, t, n), e.flags & Ku && (e.memoizedState === null ? (e = e.stateNode, (t & 335544128) === t && Zm(n, e)) : Qm(n, ju, e.memoizedState, e.memoizedProps));
				break;
			case 5:
				qu(e, t, n), e.flags & Ku && (e = e.stateNode, (t & 335544128) === t && Zm(n, e));
				break;
			case 3:
			case 4:
				var r = ju;
				ju = bm(e.stateNode.containerInfo), qu(e, t, n), ju = r;
				break;
			case 22:
				e.memoizedState === null && (r = e.alternate, r !== null && r.memoizedState !== null ? (r = Ku, Ku = 16777216, qu(e, t, n), Ku = r) : qu(e, t, n));
				break;
			case 30:
				if ((e.flags & Ku) !== 0 && (r = e.memoizedProps.name, r != null && r !== "auto")) {
					var i = e.stateNode;
					i.paired = null, Ul === null && (Ul = /* @__PURE__ */ new Map()), Ul.set(r, i);
				}
				qu(e, t, n);
				break;
			default: qu(e, t, n);
		}
	}
	function Yu(e) {
		var t = e.alternate;
		if (t !== null && (e = t.child, e !== null)) {
			t.child = null;
			do
				t = e.sibling, e.sibling = null, e = t;
			while (e !== null);
		}
	}
	function Xu(e) {
		var t = e.deletions;
		if (e.flags & 16) {
			if (t !== null) for (var n = 0; n < t.length; n++) {
				var r = t[n];
				du = r, $u(r, e);
			}
			Yu(e);
		}
		if (e.subtreeFlags & 10256) for (e = e.child; e !== null;) Zu(e), e = e.sibling;
	}
	function Zu(e) {
		switch (e.tag) {
			case 0:
			case 11:
			case 15:
				Xu(e), e.flags & 2048 && Tl(9, e, e.return);
				break;
			case 3:
				Xu(e);
				break;
			case 12:
				Xu(e);
				break;
			case 22:
				var t = e.stateNode;
				e.memoizedState !== null && t._visibility & 2 && (e.return === null || e.return.tag !== 13) ? (t._visibility &= -3, Qu(e)) : Xu(e);
				break;
			default: Xu(e);
		}
	}
	function Qu(e) {
		var t = e.deletions;
		if (e.flags & 16) {
			if (t !== null) for (var n = 0; n < t.length; n++) {
				var r = t[n];
				du = r, $u(r, e);
			}
			Yu(e);
		}
		for (e = e.child; e !== null;) {
			switch (t = e, t.tag) {
				case 0:
				case 11:
				case 15:
					Tl(8, t, t.return), Qu(t);
					break;
				case 22:
					n = t.stateNode, n._visibility & 2 && (n._visibility &= -3, Qu(t));
					break;
				default: Qu(t);
			}
			e = e.sibling;
		}
	}
	function $u(e, t) {
		for (; du !== null;) {
			var n = du;
			switch (n.tag) {
				case 0:
				case 11:
				case 15:
					Tl(8, n, t);
					break;
				case 23:
				case 22:
					if (n.memoizedState !== null && n.memoizedState.cachePool !== null) {
						var r = n.memoizedState.cachePool.pool;
						r != null && r.refCount++;
					}
					break;
				case 24: Ra(n.memoizedState.cache);
			}
			if (r = n.child, r !== null) r.return = n, du = r;
			else a: for (n = e; du !== null;) {
				r = du;
				var i = r.sibling, a = r.return;
				if (Su(r), r === n) {
					du = null;
					break a;
				}
				if (i !== null) {
					i.return = a, du = i;
					break a;
				}
				du = a;
			}
		}
	}
	var ed = {
		getCacheForType: function(e) {
			var t = Aa(Ia), n = t.data.get(e);
			return n === void 0 && (n = e(), t.data.set(e, n)), n;
		},
		cacheSignal: function() {
			return Aa(Ia).controller.signal;
		}
	}, td = typeof WeakMap == "function" ? WeakMap : Map, W = 0, G = null, K = null, q = 0, J = 0, nd = null, rd = !1, id = !1, ad = !1, od = 0, Y = 0, sd = 0, cd = 0, ld = 0, ud = 0, dd = 0, fd = null, pd = null, md = !1, hd = 0, gd = 0, _d = Infinity, vd = null, yd = null, X = 0, bd = null, xd = null, Sd = 0, Cd = 0, wd = null, Td = null, Ed = null, Dd = null, Od = null, kd = 0, Ad = null;
	function jd() {
		return W & 2 && q !== 0 ? q & -q : k.T === null ? Tt() : Pf();
	}
	function Md() {
		if (ud === 0) {
			if (!(q & 536870912) || F) {
				var e = ut;
				ut <<= 1, !(ut & 3932160) && (ut = 262144), ud = e;
			} else ud = 536870912;
		}
		return e = Io.current, e !== null && (e.flags |= 32), ud;
	}
	function Nd(e, t) {
		if (t != null) {
			var n = e.stateNode, r = n.ref;
			r === null && (r = n.ref = Pp(Ci(e.memoizedProps, n))), Dd === null && (Dd = []), Dd.push(t.bind(null, r));
		}
	}
	function Pd(e, t, n) {
		(e === G && (J === 2 || J === 9) || e.cancelPendingCommit !== null) && (Vd(e, 0), Rd(e, q, ud, !1)), M(e, n), W & 2 && e === G || (e === G && (!(W & 2) && (cd |= n), Y === 4 && Rd(e, q, ud, !1)), Ef(e));
	}
	function Fd(e, t, n) {
		if (W & 6) throw Error(i(327));
		var r = !n && !(t & 127) && (t & e.expiredLanes) === 0 || mt(e, t), a = r ? Yd(e, t) : qd(e, t, !0), o = r;
		do {
			if (a === 0) {
				id && !r && Rd(e, t, 0, !1);
				break;
			}
			if (n = e.current.alternate, o && !Ld(n)) a = qd(e, t, !1), o = !1;
			else {
				if (a === 2) {
					if (o = t, e.errorRecoveryDisabledLanes & o) var s = 0;
					else s = e.pendingLanes & -536870913, s = s === 0 ? s & 536870912 ? 536870912 : 0 : s;
					if (s !== 0) {
						t = s;
						a: {
							var c = e;
							a = fd;
							var l = c.current.memoizedState.isDehydrated;
							if (l && (Vd(c, s).flags |= 256), s = qd(c, s, !1), s !== 2 && s !== 6) {
								if (ad && !l) {
									c.errorRecoveryDisabledLanes |= o, cd |= o, a = 4;
									break a;
								}
								o = pd, pd = a, o !== null && (pd === null ? pd = o : pd.push.apply(pd, o));
							}
							a = s;
						}
						if (o = !1, a !== 2) continue;
					}
				}
				if (a === 1) {
					Vd(e, 0), Rd(e, t, 0, !0);
					break;
				}
				a: {
					switch (r = e, o = a, o) {
						case 0:
						case 1: throw Error(i(345));
						case 4: if ((t & 4194048) !== t && (t & 62914560) !== t) break;
						case 6:
							Rd(r, t, ud, !rd);
							break a;
						case 2:
							pd = null;
							break;
						case 3:
						case 5: break;
						default: throw Error(i(329));
					}
					if ((t & 62914560) === t && (a = hd + 300 - qe(), 10 < a)) {
						if (Rd(r, t, ud, !rd), pt(r, 0, !0) !== 0) break a;
						Sd = t, r.timeoutHandle = gp(Id.bind(null, r, n, pd, vd, md, t, ud, cd, dd, rd, o, "Throttled", -0, 0), a);
						break a;
					}
					Id(r, n, pd, vd, md, t, ud, cd, dd, rd, o, null, -0, 0);
				}
				break;
			}
		} while (1);
		Ef(e);
	}
	function Id(e, t, n, r, i, a, o, s, c, l, u, d, f, p) {
		e.timeoutHandle = -1;
		var m = t.subtreeFlags, h = (a & 335544064) === a;
		d = null, (h || m & 8192 || (m & 16785408) == 16785408) && (d = {
			stylesheets: null,
			count: 0,
			imgCount: 0,
			imgBytes: 0,
			suspenseyImages: [],
			waitingForImages: !0,
			waitingForViewTransition: !1,
			unsuspend: wn
		}, Ul = null, Ju(t, a, d), h && (m = d, h = e.containerInfo, h = (h.nodeType === 9 ? h : h.ownerDocument).__reactViewTransition, h != null && (m.count++, m.waitingForViewTransition = !0, m = nh.bind(m), h.finished.then(m, m))), m = (a & 62914560) === a ? hd - qe() : (a & 4194048) === a ? gd - qe() : 0, m = eh(d, m), m !== null) ? (Sd = a, e.cancelPendingCommit = m(nf.bind(null, e, t, a, n, r, i, o, s, c, l, u, d, null, f, p)), Rd(e, a, o, !l)) : nf(e, t, a, n, r, i, o, s, c, l, u, d);
	}
	function Ld(e) {
		for (var t = e;;) {
			var n = t.tag;
			if ((n === 0 || n === 11 || n === 15) && t.flags & 16384 && (n = t.updateQueue, n !== null && (n = n.stores, n !== null))) for (var r = 0; r < n.length; r++) {
				var i = n[r], a = i.getSnapshot;
				i = i.value;
				try {
					if (!qr(a(), i)) return !1;
				} catch {
					return !1;
				}
			}
			if (n = t.child, t.subtreeFlags & 16384 && n !== null) n.return = t, t = n;
			else {
				if (t === e) break;
				for (; t.sibling === null;) {
					if (t.return === null || t.return === e) return !0;
					t = t.return;
				}
				t.sibling.return = t.return, t = t.sibling;
			}
		}
		return !0;
	}
	function Rd(e, t, n, r) {
		t = ht(e, t), t &= ~ld, t &= ~cd, e.suspendedLanes |= t, e.pingedLanes &= ~t, r && (e.warmLanes |= t), r = e.expirationTimes;
		for (var i = t; 0 < i;) {
			var a = 31 - at(i), o = 1 << a;
			r[a] = -1, i &= ~o;
		}
		n !== 0 && bt(e, n, t);
	}
	function zd() {
		return W & 6 ? !0 : (Df(0, !1), !1);
	}
	function Bd() {
		if (K !== null) {
			if (J === 0) var e = K.return;
			else e = K, Sa = xa = null, cs(e), uo = null, fo = 0, e = K;
			for (; e !== null;) Cl(e.alternate, e), e = e.return;
			K = null;
		}
	}
	function Vd(e, t) {
		var n = e.timeoutHandle;
		return n !== -1 && (e.timeoutHandle = -1, _p(n)), n = e.cancelPendingCommit, n !== null && (e.cancelPendingCommit = null, n()), Sd = 0, Bd(), G = e, K = n = Bi(e.current, null), q = t, J = 0, nd = null, rd = !1, id = mt(e, t), ad = !1, dd = ud = ld = cd = sd = Y = 0, pd = fd = null, md = !1, od = ht(e, t), Ai(), n;
	}
	function Hd(e, t) {
		I = null, k.H = _c, t === eo || t === no ? (t = co(), J = 3) : t === to ? (t = co(), J = 4) : J = t === Fc ? 8 : typeof t == "object" && t && typeof t.then == "function" ? 6 : 1, nd = t, K === null && (Y = 1, kc(e, Ji(t, e.current)));
	}
	function Ud() {
		var e = Io.current;
		return e === null ? !0 : (q & 4194048) === q ? Lo === null : (q & 62914560) === q || q & 536870912 ? e === Lo : !1;
	}
	function Wd() {
		var e = k.H;
		return k.H = _c, e === null ? _c : e;
	}
	function Gd() {
		var e = k.A;
		return k.A = ed, e;
	}
	function Kd() {
		Y = 4, rd || (q & 4194048) !== q && Io.current !== null || (id = !0), !(sd & 134217727) && !(cd & 134217727) || G === null || Rd(G, q, ud, !1);
	}
	function qd(e, t, n) {
		var r = W;
		W |= 2;
		var i = Wd(), a = Gd();
		(G !== e || q !== t) && (vd = null, Vd(e, t)), t = !1;
		var o = Y;
		a: do
			try {
				if (J !== 0 && K !== null) {
					var s = K, c = nd;
					switch (J) {
						case 8:
							Bd(), o = 6;
							break a;
						case 3:
						case 2:
						case 9:
						case 6:
							Io.current === null && (t = !0);
							var l = J;
							if (J = 0, nd = null, $d(e, s, c, l), n && id) {
								o = 0;
								break a;
							}
							break;
						default: l = J, J = 0, nd = null, $d(e, s, c, l);
					}
				}
				Jd(), o = Y;
				break;
			} catch (t) {
				Hd(e, t);
			}
		while (1);
		return t && e.shellSuspendCounter++, Sa = xa = null, W = r, k.H = i, k.A = a, K === null && (G = null, q = 0, Ai()), o;
	}
	function Jd() {
		for (; K !== null;) Zd(K);
	}
	function Yd(e, t) {
		var n = W;
		W |= 2;
		var r = Wd(), a = Gd();
		G !== e || q !== t ? (vd = null, _d = qe() + 500, Vd(e, t)) : id = mt(e, t);
		a: do
			try {
				if (J !== 0 && K !== null) {
					t = K;
					var o = nd;
					b: switch (J) {
						case 1:
							J = 0, nd = null, $d(e, t, o, 1);
							break;
						case 2:
						case 9:
							if (io(o)) {
								J = 0, nd = null, Qd(t);
								break;
							}
							t = function() {
								J !== 2 && J !== 9 || G !== e || (J = 7), Ef(e);
							}, o.then(t, t);
							break a;
						case 3:
							J = 7;
							break a;
						case 4:
							J = 5;
							break a;
						case 7:
							io(o) ? (J = 0, nd = null, Qd(t)) : (J = 0, nd = null, $d(e, t, o, 7));
							break;
						case 5:
							var s = null;
							switch (K.tag) {
								case 26: s = K.memoizedState;
								case 5:
								case 27:
									var c = K;
									if (s ? Ym(s) : c.stateNode.complete) {
										J = 0, nd = null;
										var l = c.sibling;
										if (l !== null) K = l;
										else {
											var u = c.return;
											u === null ? K = null : (K = u, ef(u));
										}
										break b;
									}
							}
							J = 0, nd = null, $d(e, t, o, 5);
							break;
						case 6:
							J = 0, nd = null, $d(e, t, o, 6);
							break;
						case 8:
							Bd(), Y = 6;
							break a;
						default: throw Error(i(462));
					}
				}
				Xd();
				break;
			} catch (t) {
				Hd(e, t);
			}
		while (1);
		return Sa = xa = null, k.H = r, k.A = a, W = n, K === null ? (G = null, q = 0, Ai(), Y) : 0;
	}
	function Xd() {
		for (; K !== null && !Ge();) Zd(K);
	}
	function Zd(e) {
		var t = hl(e.alternate, e, od);
		e.memoizedProps = e.pendingProps, t === null ? ef(e) : K = t;
	}
	function Qd(e) {
		var t = e, n = t.alternate;
		switch (t.tag) {
			case 15:
			case 0:
				t = Yc(n, t, t.pendingProps, t.type, void 0, q);
				break;
			case 11:
				t = Yc(n, t, t.pendingProps, t.type.render, t.ref, q);
				break;
			case 5:
				cs(t);
				var r = t;
				r === la && (F ? (ha(r), r.tag === 5 && r.stateNode != null && (P = r.stateNode)) : (ha(r), F = !0));
			default: Cl(n, t), t = K = Vi(t, od), t = hl(n, t, od);
		}
		e.memoizedProps = e.pendingProps, t === null ? ef(e) : K = t;
	}
	function $d(e, t, n, r) {
		Sa = xa = null, cs(t), uo = null, fo = 0;
		var i = t.return;
		try {
			if (Pc(e, i, t, n, q)) {
				Y = 1, kc(e, Ji(n, e.current)), K = null;
				return;
			}
		} catch (t) {
			if (i !== null) throw K = i, t;
			Y = 1, kc(e, Ji(n, e.current)), K = null;
			return;
		}
		t.flags & 32768 ? (F || r === 1 ? e = !0 : id || q & 536870912 ? e = !1 : (rd = e = !0, (r === 2 || r === 9 || r === 3 || r === 6) && (r = Io.current, r !== null && r.tag === 13 && (r.flags |= 16384))), tf(t, e)) : ef(t);
	}
	function ef(e) {
		var t = e;
		do {
			if (t.flags & 32768) {
				tf(t, rd);
				return;
			}
			e = t.return;
			var n = xl(t.alternate, t, od);
			if (n !== null) {
				K = n;
				return;
			}
			if (t = t.sibling, t !== null) {
				K = t;
				return;
			}
			K = t = e;
		} while (t !== null);
		Y === 0 && (Y = 5);
	}
	function tf(e, t) {
		do {
			var n = Sl(e.alternate, e);
			if (n !== null) {
				n.flags &= 32767, K = n;
				return;
			}
			if (n = e.return, n !== null && (n.flags |= 32768, n.subtreeFlags = 0, n.deletions = null), !t && (e = e.sibling, e !== null)) {
				K = e;
				return;
			}
			K = e = n;
		} while (e !== null);
		Y = 6, K = null;
	}
	function nf(e, t, n, r, a, o, s, c, l, u, d, f) {
		e.cancelPendingCommit = null;
		do
			df();
		while (X !== 0);
		if (W & 6) throw Error(i(327));
		if (t !== null) {
			if (t === e.current) throw Error(i(177));
			e === G && (K = G = null, q = 0), xd = t, bd = e, Sd = n, wd = a, Td = r, rf(e, t, n, s, c, l, f);
		}
	}
	function rf(e, t, n, r, i, a, o) {
		var s = t.lanes | t.childLanes;
		if (Cd = s, s |= ki, yt(e, n, s, r, i, a), Dd = null, (n & 335544064) === n ? (Od = Va(e), r = 10262) : (Od = null, r = 10256), (t.subtreeFlags & r) !== 0 || (t.flags & r) !== 0 ? (e.callbackNode = null, e.callbackPriority = 0, yf(Ze, function() {
			return ff(), null;
		})) : (e.callbackNode = null, e.callbackPriority = 0), Hl = !1, r = !!(t.flags & 13878), t.subtreeFlags & 13878 || r) {
			r = k.T, k.T = null, i = A.p, A.p = 2, a = W, W |= 4;
			try {
				gu(e, t, n);
			} finally {
				W = a, A.p = i, k.T = r;
			}
		}
		X = 1, Hl ? Ed = Mp(o, e.containerInfo, Od, sf, cf, of, lf, ff, af, null, null) : (sf(), cf(), lf());
	}
	function af(e) {
		if (X !== 0) {
			var t = bd.onRecoverableError;
			t(e, { componentStack: null });
		}
	}
	function of() {
		X === 3 && (X = 0, Iu(xd, bd), X = 4);
	}
	function sf() {
		if (X === 1) {
			X = 0;
			var e = bd, t = xd, n = Sd, r = !!(t.flags & 13878);
			if (t.subtreeFlags & 13878 || r) {
				r = k.T, k.T = null;
				var i = A.p;
				A.p = 2;
				var a = W;
				W |= 4;
				try {
					pu = mu = !1, Mu(t, e, n), n = cp;
					var o = $r(e.containerInfo), s = n.focusedElem, c = n.selectionRange;
					if (o !== s && s && s.ownerDocument && Qr(s.ownerDocument.documentElement, s)) {
						if (c !== null && ei(s)) {
							var l = c.start, u = c.end;
							if (u === void 0 && (u = l), "selectionStart" in s) s.selectionStart = l, s.selectionEnd = Math.min(u, s.value.length);
							else {
								var d = s.ownerDocument || document, f = d && d.defaultView || window;
								if (f.getSelection) {
									var p = f.getSelection(), m = s.textContent.length, h = Math.min(c.start, m), g = c.end === void 0 ? h : Math.min(c.end, m);
									!p.extend && h > g && (o = g, g = h, h = o);
									var _ = Zr(s, h), v = Zr(s, g);
									if (_ && v && (p.rangeCount !== 1 || p.anchorNode !== _.node || p.anchorOffset !== _.offset || p.focusNode !== v.node || p.focusOffset !== v.offset)) {
										var y = d.createRange();
										y.setStart(_.node, _.offset), p.removeAllRanges(), h > g ? (p.addRange(y), p.extend(v.node, v.offset)) : (y.setEnd(v.node, v.offset), p.addRange(y));
									}
								}
							}
						}
						for (d = [], p = s; p = p.parentNode;) p.nodeType === 1 && d.push({
							element: p,
							left: p.scrollLeft,
							top: p.scrollTop
						});
						for (typeof s.focus == "function" && s.focus(), s = 0; s < d.length; s++) {
							var b = d[s];
							b.element.scrollLeft = b.left, b.element.scrollTop = b.top;
						}
					}
					gh = !!sp, cp = sp = null;
				} finally {
					W = a, A.p = i, k.T = r;
				}
			}
			e.current = t, X = 2;
		}
	}
	function cf() {
		if (X === 2) {
			X = 0;
			var e = bd, t = xd, n = !!(t.flags & 8772);
			if (t.subtreeFlags & 8772 || n) {
				n = k.T, k.T = null;
				var r = A.p;
				A.p = 2;
				var i = W;
				W |= 4;
				try {
					vu(e, t.alternate, t);
				} finally {
					W = i, A.p = r, k.T = n;
				}
			}
			X = 3;
		}
	}
	function lf() {
		if (X === 4 || X === 3) {
			X = 0;
			var e = Ed;
			Ed = null, Ke();
			var t = bd, n = xd, r = Sd, i = Td, a = (r & 335544064) === r ? 10262 : 10256;
			if ((n.subtreeFlags & a) !== 0 || (n.flags & a) !== 0 ? X = 5 : (X = 0, xd = bd = null, uf(t, t.pendingLanes)), a = t.pendingLanes, a === 0 && (yd = null), wt(r), n = n.stateNode, rt && typeof rt.onCommitFiberRoot == "function") try {
				rt.onCommitFiberRoot(nt, n, void 0, (n.current.flags & 128) == 128);
			} catch {}
			if (i !== null) {
				n = k.T, a = A.p, A.p = 2, k.T = null;
				try {
					for (var o = t.onRecoverableError, s = 0; s < i.length; s++) {
						var c = i[s];
						o(c.value, { componentStack: c.stack });
					}
				} finally {
					k.T = n, A.p = a;
				}
			}
			if (i = Dd, o = Od, Od = null, i !== null && (Dd = null, o === null && (o = []), e !== null)) for (c = 0; c < i.length; c++) n = (0, i[c])(o), n !== void 0 && e.finished.finally(n);
			Sd & 3 && df(), Ef(t), a = t.pendingLanes, r & 261930 && a & 42 ? t === Ad ? kd++ : (kd = 0, Ad = t) : (kd = 0, Ad = null), Df(0, !1);
		}
	}
	function uf(e, t) {
		(e.pooledCacheLanes &= t) === 0 && (t = e.pooledCache, t != null && (e.pooledCache = null, Ra(t)));
	}
	function df() {
		return Ed !== null && (Ed.skipTransition(), Ed = null), sf(), cf(), lf(), ff();
	}
	function ff() {
		if (X !== 5) return !1;
		var e = bd, t = Cd;
		Cd = 0;
		var n = wt(Sd), r = k.T, a = A.p;
		try {
			A.p = 32 > n ? 32 : n, k.T = null, n = wd, wd = null;
			var o = bd, s = Sd;
			if (X = 0, xd = bd = null, Sd = 0, W & 6) throw Error(i(331));
			var c = W;
			if (W |= 4, Zu(o.current), Uu(o, o.current, s, n), W = c, Df(0, !1), rt && typeof rt.onPostCommitFiberRoot == "function") try {
				rt.onPostCommitFiberRoot(nt, o);
			} catch {}
			return !0;
		} finally {
			A.p = a, k.T = r, uf(e, t);
		}
	}
	function pf(e, t, n) {
		t = Ji(n, t), t = jc(e.stateNode, t, 2), e = Co(e, t, 2), e !== null && (M(e, 2), Ef(e));
	}
	function Z(e, t, n) {
		if (e.tag === 3) pf(e, e, n);
		else for (; t !== null;) {
			if (t.tag === 3) {
				pf(t, e, n);
				break;
			}
			if (t.tag === 1) {
				var r = t.stateNode;
				if (typeof t.type.getDerivedStateFromError == "function" || typeof r.componentDidCatch == "function" && (yd === null || !yd.has(r))) {
					e = Ji(n, e), n = Mc(2), r = Co(t, n, 2), r !== null && (Nc(n, r, t, e), M(r, 2), Ef(r));
					break;
				}
			}
			t = t.return;
		}
	}
	function mf(e, t, n) {
		var r = e.pingCache;
		if (r === null) {
			r = e.pingCache = new td();
			var i = /* @__PURE__ */ new Set();
			r.set(t, i);
		} else i = r.get(t), i === void 0 && (i = /* @__PURE__ */ new Set(), r.set(t, i));
		i.has(n) || (ad = !0, i.add(n), e = hf.bind(null, e, t, n), t.then(e, e));
	}
	function hf(e, t, n) {
		var r = e.pingCache;
		r !== null && r.delete(t), e.pingedLanes |= e.suspendedLanes & n, e.warmLanes &= ~n, G === e && (q & n) === n && (Y === 4 || Y === 3 && (q & 62914560) === q && 300 > qe() - hd ? W & 2 ? ld |= n : Vd(e, 0) : ld |= n, dd === q && (dd = 0)), Ef(e);
	}
	function gf(e, t) {
		t === 0 && (t = _t()), e = Ni(e, t), e !== null && (M(e, t), Ef(e));
	}
	function _f(e) {
		var t = e.memoizedState, n = 0;
		t !== null && (n = t.retryLane), gf(e, n);
	}
	function vf(e, t) {
		var n = 0;
		switch (e.tag) {
			case 31:
			case 13:
				var r = e.stateNode, a = e.memoizedState;
				a !== null && (n = a.retryLane);
				break;
			case 19:
				r = e.stateNode;
				break;
			case 22:
				r = e.stateNode._retryCache;
				break;
			default: throw Error(i(314));
		}
		r !== null && r.delete(t), gf(e, n);
	}
	function yf(e, t) {
		return Ue(e, t);
	}
	var bf = null, xf = null, Sf = !1, Cf = !1, wf = !1, Tf = 0;
	function Ef(e) {
		e !== xf && e.next === null && (xf === null ? bf = xf = e : xf = xf.next = e), Cf = !0, Sf || (Sf = !0, Nf());
	}
	function Df(e, t) {
		if (!wf && Cf) {
			wf = !0;
			do
				for (var n = !1, r = bf; r !== null;) {
					if (!t) {
						if (e !== 0) {
							var i = r.pendingLanes;
							if (i === 0) var a = 0;
							else {
								var o = r.suspendedLanes, s = r.pingedLanes;
								a = (1 << 31 - at(42 | e) + 1) - 1, a &= i & ~(o & ~s), a = a & 201326741 ? a & 201326741 | 1 : a ? a | 2 : 0;
							}
							a !== 0 && (n = !0, Mf(r, a));
						} else a = q, a = pt(r, r === G ? a : 0, r.cancelPendingCommit !== null || r.timeoutHandle !== -1), !(a & 3) || mt(r, a) || (n = !0, Mf(r, a));
					}
					r = r.next;
				}
			while (n);
			wf = !1;
		}
	}
	function Of() {
		kf();
	}
	function kf() {
		Cf = Sf = !1;
		var e = 0;
		Tf !== 0 && hp() && (e = Tf);
		for (var t = qe(), n = null, r = bf; r !== null;) {
			var i = r.next, a = Af(r, t);
			a === 0 ? (r.next = null, n === null ? bf = i : n.next = i, i === null && (xf = n)) : (n = r, (e !== 0 || a & 3) && (Cf = !0)), r = i;
		}
		X !== 0 && X !== 5 || Df(e, !1), Tf !== 0 && (Tf = 0);
	}
	function Af(e, t) {
		for (var n = e.suspendedLanes, r = e.pingedLanes, i = e.expirationTimes, a = e.pendingLanes & -62914561; 0 < a;) {
			var o = 31 - at(a), s = 1 << o, c = i[o];
			c === -1 ? ((s & n) === 0 || (s & r) !== 0) && (i[o] = gt(s, t)) : c <= t && (e.expiredLanes |= s), a &= ~s;
		}
		if (t = G, n = q, n = pt(e, e === t ? n : 0, e.cancelPendingCommit !== null || e.timeoutHandle !== -1), r = e.callbackNode, n === 0 || e === t && (J === 2 || J === 9) || e.cancelPendingCommit !== null) return r !== null && r !== null && We(r), e.callbackNode = null, e.callbackPriority = 0;
		if (!(n & 3) || mt(e, n)) {
			if (t = n & -n, t === e.callbackPriority) return t;
			switch (r !== null && We(r), wt(n)) {
				case 2:
				case 8:
					n = Xe;
					break;
				case 32:
					n = Ze;
					break;
				case 268435456:
					n = $e;
					break;
				default: n = Ze;
			}
			return r = jf.bind(null, e), n = Ue(n, r), e.callbackPriority = t, e.callbackNode = n, t;
		}
		return r !== null && r !== null && We(r), e.callbackPriority = 2, e.callbackNode = null, 2;
	}
	function jf(e, t) {
		if (X !== 0 && X !== 5) return e.callbackNode = null, e.callbackPriority = 0, null;
		var n = e.callbackNode;
		if (df() && e.callbackNode !== n) return null;
		var r = q;
		return r = pt(e, e === G ? r : 0, e.cancelPendingCommit !== null || e.timeoutHandle !== -1), r === 0 ? null : (Fd(e, r, t), Af(e, qe()), e.callbackNode != null && e.callbackNode === n ? jf.bind(null, e) : null);
	}
	function Mf(e, t) {
		if (df()) return null;
		Fd(e, t, !0);
	}
	function Nf() {
		bp(function() {
			W & 6 ? Ue(Ye, Of) : kf();
		});
	}
	function Pf() {
		if (Tf === 0) {
			var e = Wa;
			e === 0 && (e = lt, lt <<= 1, !(lt & 261888) && (lt = 256)), Tf = e;
		}
		return Tf;
	}
	function Ff(e) {
		return e == null || typeof e == "symbol" || typeof e == "boolean" ? null : typeof e == "function" ? e : Cn(e);
	}
	function If(e, t, n, r, i) {
		if (t === "submit" && n && n.stateNode === i) {
			var a = Ff((i[kt] || null).action), o = r.submitter;
			o && (t = (t = o[kt] || null) ? Ff(t.formAction) : o.getAttribute("formAction"), t !== null && (a = t, o = null));
			var s = new Gn("action", "action", null, r, i);
			e.push({
				event: s,
				listeners: [{
					instance: null,
					listener: function() {
						if (r.defaultPrevented) {
							if (Tf !== 0) {
								var e = new FormData(i, o);
								rc(n, {
									pending: !0,
									data: e,
									method: i.method,
									action: a
								}, null, e);
							}
						} else typeof a == "function" && (s.preventDefault(), e = new FormData(i, o), rc(n, {
							pending: !0,
							data: e,
							method: i.method,
							action: a
						}, a, e));
					},
					currentTarget: i
				}]
			});
		}
	}
	for (var Lf = 0; Lf < bi.length; Lf++) {
		var Rf = bi[Lf];
		xi(Rf.toLowerCase(), "on" + (Rf[0].toUpperCase() + Rf.slice(1)));
	}
	xi(fi, "onAnimationEnd"), xi(pi, "onAnimationIteration"), xi(mi, "onAnimationStart"), xi("dblclick", "onDoubleClick"), xi("focusin", "onFocus"), xi("focusout", "onBlur"), xi(hi, "onTransitionRun"), xi(gi, "onTransitionStart"), xi(_i, "onTransitionCancel"), xi(vi, "onTransitionEnd"), qt("onMouseEnter", ["mouseout", "mouseover"]), qt("onMouseLeave", ["mouseout", "mouseover"]), qt("onPointerEnter", ["pointerout", "pointerover"]), qt("onPointerLeave", ["pointerout", "pointerover"]), Kt("onChange", "change click focusin focusout input keydown keyup selectionchange".split(" ")), Kt("onSelect", "focusout contextmenu dragend focusin keydown keyup mousedown mouseup selectionchange".split(" ")), Kt("onBeforeInput", [
		"compositionend",
		"keypress",
		"textInput",
		"paste"
	]), Kt("onCompositionEnd", "compositionend focusout keydown keypress keyup mousedown".split(" ")), Kt("onCompositionStart", "compositionstart focusout keydown keypress keyup mousedown".split(" ")), Kt("onCompositionUpdate", "compositionupdate focusout keydown keypress keyup mousedown".split(" "));
	var zf = "abort canplay canplaythrough durationchange emptied encrypted ended error loadeddata loadedmetadata loadstart pause play playing progress ratechange resize seeked seeking stalled suspend timeupdate volumechange waiting".split(" "), Bf = new Set("beforetoggle cancel close invalid load scroll scrollend toggle".split(" ").concat(zf));
	function Vf(e, t) {
		t = !!(t & 4);
		for (var n = 0; n < e.length; n++) {
			var r = e[n], i = r.event;
			r = r.listeners;
			a: {
				var a = void 0;
				if (t) for (var o = r.length - 1; 0 <= o; o--) {
					var s = r[o], c = s.instance, l = s.currentTarget;
					if (s = s.listener, c !== a && i.isPropagationStopped()) break a;
					a = s, i.currentTarget = l;
					try {
						a(i);
					} catch (e) {
						Ei(e);
					}
					i.currentTarget = null, a = c;
				}
				else for (o = 0; o < r.length; o++) {
					if (s = r[o], c = s.instance, l = s.currentTarget, s = s.listener, c !== a && i.isPropagationStopped()) break a;
					a = s, i.currentTarget = l;
					try {
						a(i);
					} catch (e) {
						Ei(e);
					}
					i.currentTarget = null, a = c;
				}
			}
		}
	}
	function Q(e, t) {
		var n = t[jt];
		n === void 0 && (n = t[jt] = /* @__PURE__ */ new Set());
		var r = e + "__bubble";
		n.has(r) || (Gf(t, e, 2, !1), n.add(r));
	}
	function Hf(e, t, n) {
		var r = 0;
		t && (r |= 4), Gf(n, e, r, t);
	}
	var Uf = "_reactListening" + Math.random().toString(36).slice(2);
	function Wf(e) {
		if (!e[Uf]) {
			e[Uf] = !0, Wt.forEach(function(t) {
				t !== "selectionchange" && (Bf.has(t) || Hf(t, !1, e), Hf(t, !0, e));
			});
			var t = e.nodeType === 9 ? e : e.ownerDocument;
			t === null || t[Uf] || (t[Uf] = !0, Hf("selectionchange", !1, t));
		}
	}
	function Gf(e, t, n, r) {
		switch (Ch(t)) {
			case 2:
				var i = _h;
				break;
			case 8:
				i = vh;
				break;
			default: i = yh;
		}
		n = i.bind(null, t, n, e), i = void 0, !Pn || t !== "touchstart" && t !== "touchmove" && t !== "wheel" || (i = !0), r ? i === void 0 ? e.addEventListener(t, n, !0) : e.addEventListener(t, n, {
			capture: !0,
			passive: i
		}) : i === void 0 ? e.addEventListener(t, n, !1) : e.addEventListener(t, n, { passive: i });
	}
	function Kf(e, t, n, r, i) {
		var a = r;
		if (!(t & 1) && !(t & 2) && r !== null) a: for (;;) {
			if (r === null) return;
			var s = r.tag;
			if (s === 3 || s === 4) {
				var c = r.stateNode.containerInfo;
				if (c === i) break;
				if (s === 4) for (s = r.return; s !== null;) {
					var l = s.tag;
					if ((l === 3 || l === 4) && s.stateNode.containerInfo === i) return;
					s = s.return;
				}
				for (; c !== null;) {
					if (s = Rt(c), s === null) return;
					if (l = s.tag, l === 5 || l === 6 || l === 26 || l === 27) {
						r = a = s;
						continue a;
					}
					c = c.parentNode;
				}
			}
			r = r.return;
		}
		jn(function() {
			var r = a, i = En(n), s = [];
			a: {
				var c = yi.get(e);
				if (c !== void 0) {
					var l = Gn, u = e;
					switch (e) {
						case "keypress": if (Bn(n) === 0) break a;
						case "keydown":
						case "keyup":
							l = lr;
							break;
						case "focusin":
							u = "focus", l = er;
							break;
						case "focusout":
							u = "blur", l = er;
							break;
						case "beforeblur":
						case "afterblur":
							l = er;
							break;
						case "click": if (n.button === 2) break a;
						case "auxclick":
						case "dblclick":
						case "mousedown":
						case "mousemove":
						case "mouseup":
						case "mouseout":
						case "mouseover":
						case "contextmenu":
							l = Qn;
							break;
						case "drag":
						case "dragend":
						case "dragenter":
						case "dragexit":
						case "dragleave":
						case "dragover":
						case "dragstart":
						case "drop":
							l = $n;
							break;
						case "touchcancel":
						case "touchend":
						case "touchmove":
						case "touchstart":
							l = fr;
							break;
						case fi:
						case pi:
						case mi:
							l = tr;
							break;
						case vi:
							l = pr;
							break;
						case "scroll":
						case "scrollend":
							l = qn;
							break;
						case "wheel":
							l = mr;
							break;
						case "copy":
						case "cut":
						case "paste":
							l = nr;
							break;
						case "gotpointercapture":
						case "lostpointercapture":
						case "pointercancel":
						case "pointerdown":
						case "pointermove":
						case "pointerout":
						case "pointerover":
						case "pointerup":
							l = ur;
							break;
						case "submit":
							l = dr;
							break;
						case "toggle":
						case "beforetoggle": l = hr;
					}
					var d = !!(t & 4), f = !d && (e === "scroll" || e === "scrollend"), p = d ? c === null ? null : c + "Capture" : c;
					d = [];
					for (var m = r, h; m !== null;) {
						var g = m;
						if (h = g.stateNode, g = g.tag, g !== 5 && g !== 26 && g !== 27 || h === null || p === null || (g = Mn(m, p), g != null && d.push(qf(m, g, h))), f) break;
						m = m.return;
					}
					0 < d.length && (c = new l(c, u, null, n, i), s.push({
						event: c,
						listeners: d
					}));
				}
			}
			if (!(t & 7)) {
				a: {
					if (l = e === "mouseover" || e === "pointerover", c = e === "mouseout" || e === "pointerout", l && n !== Tn && (u = n.relatedTarget || n.fromElement) && (Rt(u) || u[At])) break a;
					(c || l) && (u = i.window === i ? i : (l = i.ownerDocument) ? l.defaultView || l.parentWindow : window, c ? (l = n.relatedTarget || n.toElement, c = r, l = l ? Rt(l) : null, l !== null && (f = o(l), d = l.tag, l !== f || d !== 5 && d !== 27 && d !== 6) && (l = null)) : (c = null, l = r), c !== l && (d = Qn, g = "onMouseLeave", p = "onMouseEnter", m = "mouse", (e === "pointerout" || e === "pointerover") && (d = ur, g = "onPointerLeave", p = "onPointerEnter", m = "pointer"), f = c == null ? u : Bt(c), h = l == null ? u : Bt(l), u = new d(g, m + "leave", c, n, i), u.target = f, u.relatedTarget = h, g = null, Rt(i) === r && (d = new d(p, m + "enter", l, n, i), d.target = h, d.relatedTarget = f, g = d), f = g, d = c && l ? ee(c, l, Yf) : null, c !== null && Xf(s, u, c, d, !1), l !== null && f !== null && Xf(s, f, l, d, !0)));
				}
				a: {
					if (c = r ? Bt(r) : window, l = c.nodeName && c.nodeName.toLowerCase(), l === "select" || l === "input" && c.type === "file") var _ = Fr;
					else if (kr(c)) {
						if (Ir) _ = Gr;
						else {
							_ = Ur;
							var v = Hr;
						}
					} else l = c.nodeName, !l || l.toLowerCase() !== "input" || c.type !== "checkbox" && c.type !== "radio" ? r && bn(r.elementType) && (_ = Fr) : _ = Wr;
					if (_ &&= _(e, r)) {
						Ar(s, _, n, i);
						break a;
					}
					v && v(e, c, r);
				}
				switch (v = r ? Bt(r) : window, e) {
					case "focusin":
						(kr(v) || v.contentEditable === "true") && (ni = v, ri = r, ii = null);
						break;
					case "focusout":
						ii = ri = ni = null;
						break;
					case "mousedown":
						ai = !0;
						break;
					case "contextmenu":
					case "mouseup":
					case "dragend":
						ai = !1, oi(s, n, i);
						break;
					case "selectionchange": if (ti) break;
					case "keydown":
					case "keyup": oi(s, n, i);
				}
				var y;
				if (_r) b: {
					switch (e) {
						case "compositionstart":
							var b = "onCompositionStart";
							break b;
						case "compositionend":
							b = "onCompositionEnd";
							break b;
						case "compositionupdate":
							b = "onCompositionUpdate";
							break b;
					}
					b = void 0;
				}
				else Tr ? Cr(e, n) && (b = "onCompositionEnd") : e === "keydown" && n.keyCode === 229 && (b = "onCompositionStart");
				b && (br && n.locale !== "ko" && (Tr || b !== "onCompositionStart" ? b === "onCompositionEnd" && Tr && (y = zn()) : (In = i, Ln = "value" in In ? In.value : In.textContent, Tr = !0)), v = Jf(r, b), 0 < v.length && (b = new rr(b, e, null, n, i), s.push({
					event: b,
					listeners: v
				}), y ? b.data = y : (y = wr(n), y !== null && (b.data = y)))), (y = yr ? Er(e, n) : Dr(e, n)) && (b = Jf(r, "onBeforeInput"), 0 < b.length && (v = new rr("onBeforeInput", "beforeinput", null, n, i), s.push({
					event: v,
					listeners: b
				}), v.data = y)), If(s, e, r, n, i);
			}
			Vf(s, t);
		});
	}
	function qf(e, t, n) {
		return {
			instance: e,
			listener: t,
			currentTarget: n
		};
	}
	function Jf(e, t) {
		for (var n = t + "Capture", r = []; e !== null;) {
			var i = e, a = i.stateNode;
			if (i = i.tag, i !== 5 && i !== 26 && i !== 27 || a === null || (i = Mn(e, n), i != null && r.unshift(qf(e, i, a)), i = Mn(e, t), i != null && r.push(qf(e, i, a))), e.tag === 3) return r;
			e = e.return;
		}
		return [];
	}
	function Yf(e) {
		if (e === null) return null;
		do
			e = e.return;
		while (e && e.tag !== 5 && e.tag !== 27);
		return e || null;
	}
	function Xf(e, t, n, r, i) {
		for (var a = t._reactName, o = []; n !== null && n !== r;) {
			var s = n, c = s.alternate, l = s.stateNode;
			if (s = s.tag, c !== null && c === r) break;
			s !== 5 && s !== 26 && s !== 27 || l === null || (c = l, i ? (l = Mn(n, a), l != null && o.unshift(qf(n, l, c))) : i || (l = Mn(n, a), l != null && o.push(qf(n, l, c)))), n = n.return;
		}
		o.length !== 0 && e.push({
			event: t,
			listeners: o
		});
	}
	var Zf = /\r\n?/g, Qf = /\u0000|\uFFFD/g;
	function $f(e) {
		return (typeof e == "string" ? e : "" + e).replace(Zf, "\n").replace(Qf, "");
	}
	function ep(e, t) {
		return t = $f(t), $f(e) === t;
	}
	function $(e, t, n, r, a, o) {
		switch (n) {
			case "children":
				if (typeof r == "string") t === "body" || t === "textarea" && r === "" || gn(e, r);
				else if (typeof r == "number" || typeof r == "bigint") t !== "body" && gn(e, "" + r);
				else return;
				break;
			case "className":
				en(e, "class", r);
				break;
			case "tabIndex":
				en(e, "tabindex", r);
				break;
			case "dir":
			case "role":
			case "viewBox":
			case "width":
			case "height":
				en(e, n, r);
				break;
			case "style":
				yn(e, r, o);
				return;
			case "data": if (t !== "object") {
				en(e, "data", r);
				break;
			}
			case "src":
			case "href":
				if (r === "" && (t !== "a" || n !== "href") || r == null || typeof r == "function" || typeof r == "symbol" || typeof r == "boolean") {
					e.removeAttribute(n);
					break;
				}
				r = Cn(r), e.setAttribute(n, r);
				break;
			case "action":
			case "formAction":
				if (typeof r == "function") {
					e.setAttribute(n, "javascript:throw new Error('A React form was unexpectedly submitted. If you called form.submit() manually, consider using form.requestSubmit() instead. If you\\'re trying to use event.stopPropagation() in a submit event handler, consider also calling event.preventDefault().')");
					break;
				}
				if (typeof o == "function" && (n === "formAction" ? (t !== "input" && $(e, t, "name", a.name, a, null), $(e, t, "formEncType", a.formEncType, a, null), $(e, t, "formMethod", a.formMethod, a, null), $(e, t, "formTarget", a.formTarget, a, null)) : ($(e, t, "encType", a.encType, a, null), $(e, t, "method", a.method, a, null), $(e, t, "target", a.target, a, null))), r == null || typeof r == "symbol" || typeof r == "boolean") {
					e.removeAttribute(n);
					break;
				}
				r = Cn(r), e.setAttribute(n, r);
				break;
			case "onClick":
				r != null && (e.onclick = wn);
				return;
			case "onScroll":
				r != null && Q("scroll", e);
				return;
			case "onScrollEnd":
				r != null && Q("scrollend", e);
				return;
			case "dangerouslySetInnerHTML":
				if (r != null) {
					if (typeof r != "object" || !("__html" in r)) throw Error(i(61));
					if (n = r.__html, n != null) {
						if (a.children != null) throw Error(i(60));
						o?.__html !== n && (e.innerHTML = n);
					}
				}
				break;
			case "multiple":
				e.multiple = r && typeof r != "function" && typeof r != "symbol";
				break;
			case "muted":
				e.muted = r && typeof r != "function" && typeof r != "symbol";
				break;
			case "suppressContentEditableWarning":
			case "suppressHydrationWarning":
			case "defaultValue":
			case "defaultChecked":
			case "innerHTML":
			case "ref": break;
			case "autoFocus": break;
			case "xlinkHref":
				if (r == null || typeof r == "function" || typeof r == "boolean" || typeof r == "symbol") {
					e.removeAttribute("xlink:href");
					break;
				}
				n = Cn(r), e.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", n);
				break;
			case "contentEditable":
			case "spellCheck":
			case "draggable":
			case "value":
			case "autoReverse":
			case "externalResourcesRequired":
			case "focusable":
			case "preserveAlpha":
				r != null && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, r) : e.removeAttribute(n);
				break;
			case "inert":
			case "allowFullScreen":
			case "async":
			case "autoPlay":
			case "controls":
			case "credentialless":
			case "default":
			case "defer":
			case "disabled":
			case "disablePictureInPicture":
			case "disableRemotePlayback":
			case "formNoValidate":
			case "hidden":
			case "loop":
			case "noModule":
			case "noValidate":
			case "open":
			case "playsInline":
			case "readOnly":
			case "required":
			case "reversed":
			case "scoped":
			case "seamless":
			case "itemScope":
				r && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, "") : e.removeAttribute(n);
				break;
			case "capture":
			case "download":
				!0 === r ? e.setAttribute(n, "") : !1 !== r && r != null && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, r) : e.removeAttribute(n);
				break;
			case "cols":
			case "rows":
			case "size":
			case "span":
				r != null && typeof r != "function" && typeof r != "symbol" && !isNaN(r) && 1 <= r ? e.setAttribute(n, r) : e.removeAttribute(n);
				break;
			case "rowSpan":
			case "start":
				r == null || typeof r == "function" || typeof r == "symbol" || isNaN(r) ? e.removeAttribute(n) : e.setAttribute(n, r);
				break;
			case "popover":
				Q("beforetoggle", e), Q("toggle", e), $t(e, "popover", r);
				break;
			case "xlinkActuate":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:actuate", r);
				break;
			case "xlinkArcrole":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:arcrole", r);
				break;
			case "xlinkRole":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:role", r);
				break;
			case "xlinkShow":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:show", r);
				break;
			case "xlinkTitle":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:title", r);
				break;
			case "xlinkType":
				tn(e, "http://www.w3.org/1999/xlink", "xlink:type", r);
				break;
			case "xmlBase":
				tn(e, "http://www.w3.org/XML/1998/namespace", "xml:base", r);
				break;
			case "xmlLang":
				tn(e, "http://www.w3.org/XML/1998/namespace", "xml:lang", r);
				break;
			case "xmlSpace":
				tn(e, "http://www.w3.org/XML/1998/namespace", "xml:space", r);
				break;
			case "is":
				$t(e, "is", r);
				break;
			case "innerText":
			case "textContent": return;
			default: if (!(2 < n.length) || n[0] !== "o" && n[0] !== "O" || n[1] !== "n" && n[1] !== "N") n = xn.get(n) || n, $t(e, n, r);
			else return;
		}
		N = !0;
	}
	function tp(e, t, n, r, a, o) {
		switch (n) {
			case "style":
				yn(e, r, o);
				return;
			case "dangerouslySetInnerHTML":
				if (r != null) {
					if (typeof r != "object" || !("__html" in r)) throw Error(i(61));
					if (n = r.__html, n != null) {
						if (a.children != null) throw Error(i(60));
						o?.__html !== n && (e.innerHTML = n);
					}
				}
				break;
			case "children":
				if (typeof r == "string") gn(e, r);
				else if (typeof r == "number" || typeof r == "bigint") gn(e, "" + r);
				else return;
				break;
			case "onScroll":
				r != null && Q("scroll", e);
				return;
			case "onScrollEnd":
				r != null && Q("scrollend", e);
				return;
			case "onClick":
				r != null && (e.onclick = wn);
				return;
			case "suppressContentEditableWarning":
			case "suppressHydrationWarning":
			case "innerHTML":
			case "ref": return;
			case "innerText":
			case "textContent": return;
			default:
				if (!Gt.hasOwnProperty(n)) a: {
					if (n[0] === "o" && n[1] === "n" && (a = n.endsWith("Capture"), o = n.slice(2, a ? n.length - 7 : void 0), t = e[kt] || null, t = t == null ? null : t[n], typeof t == "function" && e.removeEventListener(o, t, a), typeof r == "function")) {
						typeof t != "function" && t !== null && (n in e ? e[n] = null : e.hasAttribute(n) && e.removeAttribute(n)), e.addEventListener(o, r, a);
						break a;
					}
					N = !0, n in e ? e[n] = r : !0 === r ? e.setAttribute(n, "") : $t(e, n, r);
				}
				return;
		}
		N = !0;
	}
	function np(e, t, n) {
		switch (t) {
			case "div":
			case "span":
			case "svg":
			case "path":
			case "a":
			case "g":
			case "p":
			case "li": break;
			case "img":
				Q("error", e), Q("load", e);
				var r = !1, a = !1, o;
				for (o in n) if (n.hasOwnProperty(o)) {
					var s = n[o];
					if (s != null) switch (o) {
						case "src":
							r = !0;
							break;
						case "srcSet":
							a = !0;
							break;
						case "children":
						case "dangerouslySetInnerHTML": throw Error(i(137, t));
						default: $(e, t, o, s, n, null);
					}
				}
				a && $(e, t, "srcSet", n.srcSet, n, null), r && $(e, t, "src", n.src, n, null);
				return;
			case "input":
				Q("invalid", e);
				var c = o = s = a = null, l = null, u = null;
				for (r in n) if (n.hasOwnProperty(r)) {
					var d = n[r];
					if (d != null) switch (r) {
						case "name":
							a = d;
							break;
						case "type":
							s = d;
							break;
						case "checked":
							l = d;
							break;
						case "defaultChecked":
							u = d;
							break;
						case "value":
							o = d;
							break;
						case "defaultValue":
							c = d;
							break;
						case "children":
						case "dangerouslySetInnerHTML":
							if (d != null) throw Error(i(137, t));
							break;
						default: $(e, t, r, d, n, null);
					}
				}
				dn(e, o, c, l, u, s, a, !1);
				return;
			case "select":
				for (a in Q("invalid", e), r = s = o = null, n) if (n.hasOwnProperty(a) && (c = n[a], c != null)) switch (a) {
					case "value":
						o = c;
						break;
					case "defaultValue":
						s = c;
						break;
					case "multiple": r = c;
					default: $(e, t, a, c, n, null);
				}
				t = o, n = s, e.multiple = !!r, t == null ? n != null && pn(e, !!r, n, !0) : pn(e, !!r, t, !1);
				return;
			case "textarea":
				for (s in Q("invalid", e), o = a = r = null, n) if (n.hasOwnProperty(s) && (c = n[s], c != null)) switch (s) {
					case "value":
						r = c;
						break;
					case "defaultValue":
						a = c;
						break;
					case "children":
						o = c;
						break;
					case "dangerouslySetInnerHTML":
						if (c != null) throw Error(i(91));
						break;
					default: $(e, t, s, c, n, null);
				}
				hn(e, r, a, o);
				return;
			case "option":
				for (l in n) if (n.hasOwnProperty(l) && (r = n[l], r != null)) switch (l) {
					case "selected":
						e.selected = r && typeof r != "function" && typeof r != "symbol";
						break;
					default: $(e, t, l, r, n, null);
				}
				return;
			case "dialog":
				Q("beforetoggle", e), Q("toggle", e), Q("cancel", e), Q("close", e);
				break;
			case "iframe":
			case "object":
				Q("load", e);
				break;
			case "video":
			case "audio":
				for (r = 0; r < zf.length; r++) Q(zf[r], e);
				break;
			case "image":
				Q("error", e), Q("load", e);
				break;
			case "details":
				Q("toggle", e);
				break;
			case "embed":
			case "source":
			case "link": Q("error", e), Q("load", e);
			case "area":
			case "base":
			case "br":
			case "col":
			case "hr":
			case "keygen":
			case "meta":
			case "param":
			case "track":
			case "wbr":
			case "menuitem":
				for (u in n) if (n.hasOwnProperty(u) && (r = n[u], r != null)) switch (u) {
					case "children":
					case "dangerouslySetInnerHTML": throw Error(i(137, t));
					default: $(e, t, u, r, n, null);
				}
				return;
			default: if (bn(t)) {
				for (d in n) n.hasOwnProperty(d) && (r = n[d], r !== void 0 && tp(e, t, d, r, n, void 0));
				return;
			}
		}
		for (c in n) n.hasOwnProperty(c) && (r = n[c], r != null && $(e, t, c, r, n, null));
	}
	var rp = {};
	function ip(e, t, n, r) {
		switch (t) {
			case "div":
			case "span":
			case "svg":
			case "path":
			case "a":
			case "g":
			case "p":
			case "li": break;
			case "input":
				var a = null, o = null, s = null, c = null, l = null, u = null, d = null;
				for (m in n) {
					var f = n[m];
					if (n.hasOwnProperty(m) && f != null) switch (m) {
						case "checked": break;
						case "value": break;
						case "defaultValue": l = f;
						default: r.hasOwnProperty(m) || $(e, t, m, null, r, f);
					}
				}
				for (var p in r) {
					var m = r[p];
					if (f = n[p], r.hasOwnProperty(p) && (m != null || f != null)) switch (p) {
						case "type":
							m !== f && (N = !0), o = m;
							break;
						case "name":
							m !== f && (N = !0), a = m;
							break;
						case "checked":
							m !== f && (N = !0), u = m;
							break;
						case "defaultChecked":
							m !== f && (N = !0), d = m;
							break;
						case "value":
							m !== f && (N = !0), s = m;
							break;
						case "defaultValue":
							m !== f && (N = !0), c = m;
							break;
						case "children":
						case "dangerouslySetInnerHTML":
							if (m != null) throw Error(i(137, t));
							break;
						default: m !== f && $(e, t, p, m, r, f);
					}
				}
				un(e, s, c, l, u, d, o, a);
				return;
			case "select":
				for (o in m = s = c = p = null, n) if (l = n[o], n.hasOwnProperty(o) && l != null) switch (o) {
					case "value": break;
					case "multiple": m = l;
					default: r.hasOwnProperty(o) || $(e, t, o, null, r, l);
				}
				for (a in r) if (o = r[a], l = n[a], r.hasOwnProperty(a) && (o != null || l != null)) switch (a) {
					case "value":
						o !== l && (N = !0), p = o;
						break;
					case "defaultValue":
						o !== l && (N = !0), c = o;
						break;
					case "multiple": o !== l && (N = !0), s = o;
					default: o !== l && $(e, t, a, o, r, l);
				}
				t = c, n = s, r = m, p == null ? !!r != !!n && (t == null ? pn(e, !!n, n ? [] : "", !1) : pn(e, !!n, t, !0)) : pn(e, !!n, p, !1);
				return;
			case "textarea":
				for (c in m = p = null, n) if (a = n[c], n.hasOwnProperty(c) && a != null && !r.hasOwnProperty(c)) switch (c) {
					case "value": break;
					case "children": break;
					default: $(e, t, c, null, r, a);
				}
				for (s in r) if (a = r[s], o = n[s], r.hasOwnProperty(s) && (a != null || o != null)) switch (s) {
					case "value":
						a !== o && (N = !0), p = a;
						break;
					case "defaultValue":
						a !== o && (N = !0), m = a;
						break;
					case "children": break;
					case "dangerouslySetInnerHTML":
						if (a != null) throw Error(i(91));
						break;
					default: a !== o && $(e, t, s, a, r, o);
				}
				mn(e, p, m);
				return;
			case "option":
				for (var h in n) if (p = n[h], n.hasOwnProperty(h) && p != null && !r.hasOwnProperty(h)) switch (h) {
					case "selected":
						e.selected = !1;
						break;
					default: $(e, t, h, null, r, p);
				}
				for (l in r) if (p = r[l], m = n[l], r.hasOwnProperty(l) && p !== m && (p != null || m != null)) switch (l) {
					case "selected":
						p !== m && (N = !0), e.selected = p && typeof p != "function" && typeof p != "symbol";
						break;
					default: $(e, t, l, p, r, m);
				}
				return;
			case "img":
			case "link":
			case "area":
			case "base":
			case "br":
			case "col":
			case "embed":
			case "hr":
			case "keygen":
			case "meta":
			case "param":
			case "source":
			case "track":
			case "wbr":
			case "menuitem":
				for (var g in n) p = n[g], n.hasOwnProperty(g) && p != null && !r.hasOwnProperty(g) && $(e, t, g, null, r, p);
				for (u in r) if (p = r[u], m = n[u], r.hasOwnProperty(u) && p !== m && (p != null || m != null)) switch (u) {
					case "children":
					case "dangerouslySetInnerHTML":
						if (p != null) throw Error(i(137, t));
						break;
					default: $(e, t, u, p, r, m);
				}
				return;
			default: if (bn(t)) {
				for (var _ in n) p = n[_], n.hasOwnProperty(_) && p !== void 0 && !r.hasOwnProperty(_) && tp(e, t, _, void 0, r, p);
				for (d in r) p = r[d], m = n[d], !r.hasOwnProperty(d) || p === m || p === void 0 && m === void 0 || tp(e, t, d, p, r, m);
				return;
			}
		}
		for (var v in n) p = n[v], n.hasOwnProperty(v) && p != null && !r.hasOwnProperty(v) && $(e, t, v, null, r, p);
		for (f in r) p = r[f], m = n[f], !r.hasOwnProperty(f) || p === m || p == null && m == null || $(e, t, f, p, r, m);
	}
	function ap(e) {
		switch (e) {
			case "css":
			case "script":
			case "font":
			case "img":
			case "image":
			case "input":
			case "link": return !0;
			default: return !1;
		}
	}
	function op() {
		if (typeof performance.getEntriesByType == "function") {
			for (var e = 0, t = 0, n = performance.getEntriesByType("resource"), r = 0; r < n.length; r++) {
				var i = n[r], a = i.transferSize, o = i.initiatorType, s = i.duration;
				if (a && s && ap(o)) {
					for (o = 0, s = i.responseEnd, r += 1; r < n.length; r++) {
						var c = n[r], l = c.startTime;
						if (l > s) break;
						var u = c.transferSize, d = c.initiatorType;
						u && ap(d) && (c = c.responseEnd, o += u * (c < s ? 1 : (s - l) / (c - l)));
					}
					if (--r, t += 8 * (a + o) / (i.duration / 1e3), e++, 10 < e) break;
				}
			}
			if (0 < e) return t / e / 1e6;
		}
		return navigator.connection && (e = navigator.connection.downlink, typeof e == "number") ? e : 5;
	}
	var sp = null, cp = null;
	function lp(e) {
		return e.nodeType === 9 ? e : e.ownerDocument;
	}
	function up(e) {
		switch (e) {
			case "http://www.w3.org/2000/svg": return 1;
			case "http://www.w3.org/1998/Math/MathML": return 2;
			default: return 0;
		}
	}
	function dp(e, t) {
		if (e === 0) switch (t) {
			case "svg": return 1;
			case "math": return 2;
			default: return 0;
		}
		return e === 1 && t === "foreignObject" ? 0 : e;
	}
	function fp(e, t, n, r) {
		return n = lp(n).createElement(e), n[Ot] = r, n[kt] = t, np(n, e, t), Ht(n), n;
	}
	function pp(e, t) {
		return e === "textarea" || e === "noscript" || typeof t.children == "string" || typeof t.children == "number" || typeof t.children == "bigint" || typeof t.dangerouslySetInnerHTML == "object" && t.dangerouslySetInnerHTML !== null && t.dangerouslySetInnerHTML.__html != null;
	}
	var mp = null;
	function hp() {
		var e = window.event;
		return e && e.type === "popstate" ? e !== mp && (mp = e, !0) : (mp = null, !1);
	}
	var gp = typeof setTimeout == "function" ? setTimeout : void 0, _p = typeof clearTimeout == "function" ? clearTimeout : void 0, vp = typeof Promise == "function" ? Promise : void 0, yp = typeof requestAnimationFrame == "function" ? requestAnimationFrame : gp, bp = typeof queueMicrotask == "function" ? queueMicrotask : vp === void 0 ? gp : function(e) {
		return vp.resolve(null).then(e).catch(xp);
	};
	function xp(e) {
		setTimeout(function() {
			throw e;
		});
	}
	function Sp(e) {
		return e === "head";
	}
	function Cp(e, t) {
		var n = t, r = 0;
		do {
			var i = n.nextSibling;
			if (e.removeChild(n), i && i.nodeType === 8) {
				if (n = i.data, n === "/$" || n === "/&") {
					if (r === 0) {
						e.removeChild(i), Hh(t);
						return;
					}
					r--;
				} else if (n === "$" || n === "$?" || n === "$~" || n === "$!" || n === "&") r++;
				else if (n === "html") _m(e.ownerDocument.documentElement);
				else if (n === "head") {
					n = e.ownerDocument.head, _m(n);
					for (var a = n.firstChild; a;) {
						var o = a.nextSibling, s = a.nodeName;
						a[Ft] || s === "SCRIPT" || s === "STYLE" || s === "LINK" && a.rel.toLowerCase() === "stylesheet" || n.removeChild(a), a = o;
					}
				} else n === "body" && _m(e.ownerDocument.body);
			}
			n = i;
		} while (n);
		Hh(t);
	}
	function wp(e, t) {
		var n = e;
		e = 0;
		do {
			var r = n.nextSibling;
			if (n.nodeType === 1 ? t ? (n._stashedDisplay = n.style.display, n.style.display = "none") : (n.style.display = n._stashedDisplay || "", n.getAttribute("style") === "" && n.removeAttribute("style")) : n.nodeType === 3 && (t ? (n._stashedText = n.nodeValue, n.nodeValue = "") : n.nodeValue = n._stashedText || ""), r && r.nodeType === 8) {
				if (n = r.data, n === "/$") {
					if (e === 0) break;
					e--;
				} else n !== "$" && n !== "$?" && n !== "$~" && n !== "$!" || e++;
			}
			n = r;
		} while (n);
	}
	function Tp(e, t, n) {
		if (t = CSS.escape(t) === t ? t : "r-" + btoa(t).replace(/=/g, ""), e.style.viewTransitionName = t, n != null && (e.style.viewTransitionClass = n), n = getComputedStyle(e), n.display === "inline") {
			if (t = e.getClientRects(), t.length === 1) var r = 1;
			else for (var i = r = 0; i < t.length; i++) {
				var a = t[i];
				0 < a.width && 0 < a.height && r++;
			}
			r === 1 && (e = e.style, e.display = t.length === 1 ? "inline-block" : "block", e.marginTop = "-" + n.paddingTop, e.marginBottom = "-" + n.paddingBottom);
		}
	}
	function Ep(e, t) {
		e = e.style, t = t.style;
		var n = t == null ? null : t.hasOwnProperty("viewTransitionName") ? t.viewTransitionName : t.hasOwnProperty("view-transition-name") ? t["view-transition-name"] : null;
		e.viewTransitionName = n == null || typeof n == "boolean" ? "" : ("" + n).trim(), n = t == null ? null : t.hasOwnProperty("viewTransitionClass") ? t.viewTransitionClass : t.hasOwnProperty("view-transition-class") ? t["view-transition-class"] : null, e.viewTransitionClass = n == null || typeof n == "boolean" ? "" : ("" + n).trim(), e.display === "inline-block" && (t == null ? e.display = e.margin = "" : (n = t.display, e.display = n == null || typeof n == "boolean" ? "" : n, n = t.margin, n == null ? (n = t.hasOwnProperty("marginTop") ? t.marginTop : t["margin-top"], e.marginTop = n == null || typeof n == "boolean" ? "" : n, t = t.hasOwnProperty("marginBottom") ? t.marginBottom : t["margin-bottom"], e.marginBottom = t == null || typeof t == "boolean" ? "" : t) : e.margin = n));
	}
	function Dp(e, t, n) {
		return n = n.ownerDocument.defaultView, {
			rect: e,
			abs: t.position === "absolute" || t.position === "fixed",
			clip: t.clipPath !== "none" || t.overflow !== "visible" || t.filter !== "none" || t.mask !== "none" || t.mask !== "none" || t.borderRadius !== "0px",
			view: 0 <= e.bottom && 0 <= e.right && e.top <= n.innerHeight && e.left <= n.innerWidth
		};
	}
	function Op(e) {
		return Dp(e.getBoundingClientRect(), getComputedStyle(e), e);
	}
	function kp(e) {
		var t = e.getBoundingClientRect();
		t = new DOMRect(t.x + 2e4, t.y + 2e4, t.width, t.height);
		var n = getComputedStyle(e);
		return Dp(t, n, e);
	}
	function Ap(e) {
		return e.documentElement.clientHeight;
	}
	function jp(e) {
		this.addEventListener("load", e), this.addEventListener("error", e);
	}
	function Mp(e, t, n, r, i, a, o, s, c) {
		var l = t.nodeType === 9 ? t : t.ownerDocument;
		try {
			var u = l.startViewTransition({
				update: function() {
					var t = l.defaultView, n = t.navigation && t.navigation.transition, o = l.fonts.status;
					r();
					var s = [];
					if (o === "loaded" && (Ap(l), l.fonts.status === "loading" && s.push(l.fonts.ready)), o = s.length, e !== null) for (var c = e.suspenseyImages, u = 0, d = 0; d < c.length; d++) {
						var f = c[d];
						if (!f.complete) {
							var p = f.getBoundingClientRect();
							if (0 < p.bottom && 0 < p.right && p.top < t.innerHeight && p.left < t.innerWidth) {
								if (u += Xm(f), u > $m) {
									s.length = o;
									break;
								}
								f = new Promise(jp.bind(f)), s.push(f);
							}
						}
					}
					if (0 < s.length) return t = Promise.race([Promise.all(s), new Promise(function(e) {
						return setTimeout(e, 500);
					})]).then(i, i), (n ? Promise.allSettled([n.finished, t]) : t).then(a, a);
					if (i(), n) return n.finished.then(a, a);
					a();
				},
				types: n
			});
			l.__reactViewTransition = u;
			var d = [];
			return u.ready.then(function() {
				for (var e = l.documentElement.getAnimations({ subtree: !0 }), t = 0; t < e.length; t++) {
					var n = e[t], r = n.effect, i = r.pseudoElement;
					if (i != null && i.startsWith("::view-transition")) {
						d.push(n), n = r.getKeyframes();
						for (var a = i = void 0, s = !0, c = 0; c < n.length; c++) {
							var u = n[c], f = u.width;
							if (i === void 0) i = f;
							else if (i !== f) {
								s = !1;
								break;
							}
							if (f = u.height, a === void 0) a = f;
							else if (a !== f) {
								s = !1;
								break;
							}
							delete u.width, delete u.height, u.transform === "none" && delete u.transform;
						}
						s && i !== void 0 && a !== void 0 && (r.setKeyframes(n), s = getComputedStyle(r.target, r.pseudoElement), s.width !== i || s.height !== a) && (s = n[0], s.width = i, s.height = a, s = n[n.length - 1], s.width = i, s.height = a, r.setKeyframes(n));
					}
				}
				o();
			}, function(e) {
				l.__reactViewTransition === u && (l.__reactViewTransition = null);
				try {
					if (typeof e == "object" && e) switch (e.name) {
						case "InvalidStateError": (e.message === "View transition was skipped because document visibility state is hidden." || e.message === "Skipping view transition because document visibility state has become hidden." || e.message === "Skipping view transition because viewport size changed." || e.message === "Transition was aborted because of invalid state") && (e = null);
					}
					e !== null && c(e);
				} finally {
					r(), i(), o();
				}
			}), u.finished.finally(function() {
				for (var e = 0; e < d.length; e++) d[e].cancel();
				l.__reactViewTransition === u && (l.__reactViewTransition = null), s();
			}), u;
		} catch {
			return r(), i(), o(), null;
		}
	}
	function Np(e, t) {
		this._scope = document.documentElement, this._selector = "::view-transition-" + e + "(" + t + ")";
	}
	Np.prototype.animate = function(e, t) {
		return t = typeof t == "number" ? { duration: t } : E({}, t), t.pseudoElement = this._selector, this._scope.animate(e, t);
	}, Np.prototype.getAnimations = function() {
		for (var e = this._scope, t = this._selector, n = e.getAnimations({ subtree: !0 }), r = [], i = 0; i < n.length; i++) {
			var a = n[i].effect;
			a !== null && a.target === e && a.pseudoElement === t && r.push(n[i]);
		}
		return r;
	}, Np.prototype.getComputedStyle = function() {
		return getComputedStyle(this._scope, this._selector);
	};
	function Pp(e) {
		return {
			name: e,
			group: new Np("group", e),
			imagePair: new Np("image-pair", e),
			old: new Np("old", e),
			new: new Np("new", e)
		};
	}
	function Fp(e) {
		this._fragmentFiber = e, this._observers = this._eventListeners = null;
	}
	Fp.prototype.addEventListener = function(e, t, n) {
		var r = null, i = null;
		if (!(n != null && typeof n != "boolean" && (r = n.signal || null, r !== null && r.aborted))) {
			this._eventListeners === null && (this._eventListeners = []);
			var a = this._eventListeners;
			if (Bp(a, e, t, n) === -1) {
				var o = this, s = t;
				n != null && typeof n != "boolean" && !0 === n.once && (s = function(r) {
					o.removeEventListener(e, t, n), typeof t == "function" ? t.call(this, r) : t.handleEvent(r);
				}), r !== null && (i = o.removeEventListener.bind(o, e, t, n), r.addEventListener("abort", i, { once: !0 }), i = r.removeEventListener.bind(r, "abort", i)), r = Rp(n), a.push({
					type: e,
					listener: t,
					optionsOrUseCapture: n,
					attachedListener: s,
					cleanup: i
				}), h(this._fragmentFiber.child, !1, Ip, e, s, r);
			}
			this._eventListeners = a;
		}
	};
	function Ip(e, t, n, r) {
		return b(e).addEventListener(t, n, r), !1;
	}
	Fp.prototype.removeEventListener = function(e, t, n) {
		var r = this._eventListeners;
		if (r !== null && (t = Bp(r, e, t, n), t !== -1)) {
			var i = r[t];
			n = i.attachedListener;
			var a = i.cleanup;
			i = Rp(i.optionsOrUseCapture), h(this._fragmentFiber.child, !1, Lp, e, n, i), r.splice(t, 1), a !== null && a();
		}
	};
	function Lp(e, t, n, r) {
		return b(e).removeEventListener(t, n, r), !1;
	}
	function Rp(e) {
		return e != null && typeof e != "boolean" && (!0 === e.once || e.signal instanceof AbortSignal) ? {
			capture: e.capture,
			passive: e.passive
		} : e;
	}
	function zp(e) {
		return e == null ? "c=0" : typeof e == "boolean" ? "c=" + (e ? "1" : "0") : "c=" + (e.capture ? "1" : "0");
	}
	function Bp(e, t, n, r) {
		if (e.length === 0) return -1;
		r = zp(r);
		for (var i = 0; i < e.length; i++) {
			var a = e[i];
			if (a.type === t && a.listener === n && zp(a.optionsOrUseCapture) === r) return i;
		}
		return -1;
	}
	Fp.prototype.dispatchEvent = function(e) {
		var t = g(this._fragmentFiber);
		if (t === null) return !0;
		t = b(t);
		var n = this._eventListeners;
		if (n !== null && 0 < n.length || !e.bubbles) {
			var r = t.nodeType === 9 ? t.createComment("") : document.createTextNode("");
			if (n) for (var i = 0; i < n.length; i++) {
				var a = n[i];
				r.addEventListener(a.type, a.attachedListener, Rp(a.optionsOrUseCapture));
			}
			if (t.appendChild(r), e = r.dispatchEvent(e), n) for (i = 0; i < n.length; i++) a = n[i], r.removeEventListener(a.type, a.attachedListener, Rp(a.optionsOrUseCapture));
			return t.removeChild(r), e;
		}
		return t.dispatchEvent(e);
	}, Fp.prototype.focus = function(e) {
		h(this._fragmentFiber.child, !0, Vp, e, void 0, void 0);
	};
	function Vp(e, t) {
		return e.tag !== 6 && (e = b(e), pm(e, t));
	}
	Fp.prototype.focusLast = function(e) {
		var t = [];
		h(this._fragmentFiber.child, !0, Hp, t, void 0, void 0);
		for (var n = t.length - 1; 0 <= n && !Vp(t[n], e); n--);
	};
	function Hp(e, t) {
		return t.push(e), !1;
	}
	Fp.prototype.blur = function() {
		var e = g(this._fragmentFiber);
		e !== null && (e = b(e), e = lp(e).activeElement, e !== null && h(this._fragmentFiber.child, !1, Up, e, void 0, void 0));
	};
	function Up(e, t) {
		return e.tag !== 6 && (e = b(e), e === t || e.contains(t) ? (t.blur(), !0) : !1);
	}
	Fp.prototype.observeUsing = function(e) {
		this._observers === null && (this._observers = /* @__PURE__ */ new Set()), this._observers.add(e), h(this._fragmentFiber.child, !1, Wp, e, void 0, void 0);
	};
	function Wp(e, t) {
		return e.tag === 6 || (e = b(e), t.observe(e)), !1;
	}
	Fp.prototype.unobserveUsing = function(e) {
		var t = this._observers;
		if (t !== null && t.has(e)) {
			t.delete(e), h(this._fragmentFiber.child, !1, Gp, e, void 0, void 0);
			for (var n = t = 0; n < Kp.length; n++) {
				var r = Kp[n];
				r.fragmentInstance === this && r.observer === e ? e.unobserve(r.instance) : Kp[t++] = r;
			}
			Kp.length = t;
		}
	};
	function Gp(e, t) {
		return e.tag === 6 || (e = b(e), t.unobserve(e)), !1;
	}
	var Kp = [], qp = !1;
	function Jp(e, t, n) {
		Kp.push({
			fragmentInstance: e,
			observer: t,
			instance: n
		}), qp || (qp = !0, mm(function() {
			qp = !1;
			var e = Kp;
			Kp = [];
			for (var t = 0; t < e.length; t++) {
				var n = e[t];
				n.observer.unobserve(n.instance);
			}
		}));
	}
	Fp.prototype.getClientRects = function() {
		var e = [];
		return h(this._fragmentFiber.child, !1, Yp, e, void 0, void 0), e;
	};
	function Yp(e, t) {
		if (e.tag === 6) {
			e = e.stateNode;
			var n = e.ownerDocument.createRange();
			n.selectNodeContents(e), t.push.apply(t, n.getClientRects());
		} else e = b(e), t.push.apply(t, e.getClientRects());
		return !1;
	}
	Fp.prototype.getRootNode = function(e) {
		var t = g(this._fragmentFiber);
		return t === null ? this : b(t).getRootNode(e);
	}, Fp.prototype.compareDocumentPosition = function(e) {
		var t = g(this._fragmentFiber);
		if (t === null) return Node.DOCUMENT_POSITION_DISCONNECTED;
		var n = [];
		h(this._fragmentFiber.child, !1, Hp, n, void 0, void 0);
		var r = b(t);
		if (n.length === 0) {
			if (n = r, _(this._fragmentFiber)) {
				a: {
					for (t = this._fragmentFiber.return; t !== null;) {
						if (t.tag === 4) {
							t = t.stateNode.containerInfo;
							break a;
						}
						if (t.tag === 3 || t.tag === 5 || t.tag === 27) break;
						t = t.return;
					}
					t = null;
				}
				t != null && (n = t);
			}
			t = this._fragmentFiber;
			var i = r = n.compareDocumentPosition(e);
			return n === e ? i = Node.DOCUMENT_POSITION_CONTAINS : r & Node.DOCUMENT_POSITION_CONTAINED_BY && (n = v(t)[1], n === null ? i = Node.DOCUMENT_POSITION_PRECEDING : (e = b(n).compareDocumentPosition(e), i = e === 0 || e & Node.DOCUMENT_POSITION_FOLLOWING ? Node.DOCUMENT_POSITION_FOLLOWING : Node.DOCUMENT_POSITION_PRECEDING)), i |= Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC;
		}
		t = b(n[0]), i = b(n[n.length - 1]);
		var a = _(this._fragmentFiber) ? t.parentElement : r;
		if (a == null) return Node.DOCUMENT_POSITION_DISCONNECTED;
		r = a.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_CONTAINED_BY, a = a.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_CONTAINED_BY;
		var o = t.compareDocumentPosition(e), s = i.compareDocumentPosition(e), c = o & Node.DOCUMENT_POSITION_CONTAINED_BY || s & Node.DOCUMENT_POSITION_CONTAINED_BY;
		return s = r && a && o & Node.DOCUMENT_POSITION_FOLLOWING && s & Node.DOCUMENT_POSITION_PRECEDING, t = r && t === e || a && i === e || c || s ? Node.DOCUMENT_POSITION_CONTAINED_BY : !r && t === e || !a && i === e ? Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC : o, t & Node.DOCUMENT_POSITION_DISCONNECTED || t & Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC || Xp(t, this._fragmentFiber, n[0], n[n.length - 1], e) ? t : Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC;
	};
	function Xp(e, t, n, r, i) {
		var a = Rt(i);
		if (e & Node.DOCUMENT_POSITION_CONTAINED_BY) {
			if (n = !!a) a: {
				for (; a !== null;) {
					if (a.tag === 7 && (a === t || a.alternate === t)) {
						n = !0;
						break a;
					}
					a = a.return;
				}
				n = !1;
			}
			return n;
		}
		if (e & Node.DOCUMENT_POSITION_CONTAINS) {
			if (a === null) return a = i.ownerDocument, i === a || i === a.documentElement || i === a.body;
			a: {
				for (a = t, t = g(t); a !== null;) {
					if (!(a.tag !== 5 && a.tag !== 3 && a.tag !== 27 || a !== t && a.alternate !== t)) {
						a = !0;
						break a;
					}
					a = a.return;
				}
				a = !1;
			}
			return a;
		}
		return e & Node.DOCUMENT_POSITION_PRECEDING ? ((t = !!a) && !(t = a === n) && (t = ee(n, a, T), t === null ? t = !1 : (h(t, !0, C, a, n), a = x, x = null, t = a !== null)), t) : e & Node.DOCUMENT_POSITION_FOLLOWING ? ((t = !!a) && !(t = a === r) && (t = ee(r, a, T), t === null ? t = !1 : (h(t, !0, w, a, r), a = x, S = x = null, t = a !== null)), t) : !1;
	}
	function Zp(e, t) {
		var n = e.ownerDocument.createRange();
		n.selectNodeContents(e), e = n.getBoundingClientRect(), window.scrollTo(window.scrollX + e.left, t ? window.scrollY + e.top : window.scrollY + e.bottom - window.innerHeight);
	}
	Fp.prototype.scrollIntoView = function(e) {
		if (typeof e == "object") throw Error(i(566));
		var t = [];
		h(this._fragmentFiber.child, !1, Hp, t, void 0, void 0);
		var n = !1 !== e;
		if (t.length === 0) {
			var r = v(this._fragmentFiber);
			if (r = n ? r[1] || r[0] || g(this._fragmentFiber) : r[0] || r[1], r === null) return;
			if (r.tag === 6) {
				e = b(r), Zp(e, n);
				return;
			}
			if (r = b(r), r.nodeType !== 9) {
				if (r.nodeType === 11) {
					n = "host" in r ? r.host : null, n !== null && n.scrollIntoView(e);
					return;
				}
				r.scrollIntoView(e);
			}
		}
		for (r = n ? t.length - 1 : 0; r !== (n ? -1 : t.length);) {
			var a = t[r];
			a.tag === 6 ? (a = b(a), Zp(a, n)) : b(a).scrollIntoView(e), r += n ? -1 : 1;
		}
	};
	function Qp(e, t) {
		return e = b(e), $p(e, t), !1;
	}
	function $p(e, t) {
		e.reactFragments ??= /* @__PURE__ */ new Set(), e.reactFragments.add(t);
	}
	function em(e, t) {
		var n = t._eventListeners;
		if (n !== null) for (var r = 0; r < n.length; r++) {
			var i = n[r];
			e.addEventListener(i.type, i.attachedListener, Rp(i.optionsOrUseCapture));
		}
		e.nodeType !== 3 && (n = t._observers, n !== null && n.forEach(function(n) {
			for (var r = 0, i = 0; i < Kp.length; i++) {
				var a = Kp[i];
				(a.fragmentInstance !== t || a.observer !== n || a.instance !== e) && (Kp[r++] = a);
			}
			Kp.length = r, n.observe(e);
		}), $p(e, t));
	}
	function tm(e, t) {
		var n = t._eventListeners;
		if (n !== null) for (var r = 0; r < n.length; r++) {
			var i = n[r];
			e.removeEventListener(i.type, i.attachedListener, Rp(i.optionsOrUseCapture));
		}
		e.nodeType !== 3 && (n = t._observers, n !== null && n.forEach(function(n) {
			typeof n.rootMargin == "string" ? Jp(t, n, e) : n.unobserve(e);
		}), e.reactFragments != null && e.reactFragments.delete(t));
	}
	function nm(e) {
		var t = e.firstChild;
		for (t && t.nodeType === 10 && (t = t.nextSibling); t;) {
			var n = t;
			switch (t = t.nextSibling, n.nodeName) {
				case "HTML":
				case "HEAD":
				case "BODY":
					nm(n), Lt(n);
					continue;
				case "SCRIPT":
				case "STYLE": continue;
				case "LINK": if (n.rel.toLowerCase() === "stylesheet") continue;
			}
			e.removeChild(n);
		}
	}
	function rm(e, t, n, r) {
		for (; e.nodeType === 1;) {
			var i = n;
			if (e.nodeName.toLowerCase() !== t.toLowerCase()) {
				if (!r && (e.nodeName !== "INPUT" || e.type !== "hidden")) break;
			} else if (!r) {
				if (t === "input" && e.type === "hidden") {
					var a = i.name == null ? null : "" + i.name;
					if (i.type === "hidden" && e.getAttribute("name") === a) return e;
				} else return e;
			} else if (!e[Ft]) switch (t) {
				case "meta":
					if (!e.hasAttribute("itemprop")) break;
					return e;
				case "link":
					if (a = e.getAttribute("rel"), a === "stylesheet" && e.hasAttribute("data-precedence") || a !== i.rel || e.getAttribute("href") !== (i.href == null || i.href === "" ? null : i.href) || e.getAttribute("crossorigin") !== (i.crossOrigin == null ? null : i.crossOrigin) || e.getAttribute("title") !== (i.title == null ? null : i.title)) break;
					return e;
				case "style":
					if (e.hasAttribute("data-precedence")) break;
					return e;
				case "script":
					if (a = e.getAttribute("src"), (a !== (i.src == null ? null : i.src) || e.getAttribute("type") !== (i.type == null ? null : i.type) || e.getAttribute("crossorigin") !== (i.crossOrigin == null ? null : i.crossOrigin)) && a && e.hasAttribute("async") && !e.hasAttribute("itemprop")) break;
					return e;
				default: return e;
			}
			if (e = lm(e.nextSibling), e === null) break;
		}
		return null;
	}
	function im(e, t, n) {
		if (t === "") return null;
		for (; e.nodeType !== 3;) if ((e.nodeType !== 1 || e.nodeName !== "INPUT" || e.type !== "hidden") && !n || (e = lm(e.nextSibling), e === null)) return null;
		return e;
	}
	function am(e, t) {
		for (; e.nodeType !== 8;) if ((e.nodeType !== 1 || e.nodeName !== "INPUT" || e.type !== "hidden") && !t || (e = lm(e.nextSibling), e === null)) return null;
		return e;
	}
	function om(e) {
		return e.data === "$?" || e.data === "$~";
	}
	function sm(e) {
		return e.data === "$!" || e.data === "$?" && e.ownerDocument.readyState !== "loading";
	}
	function cm(e, t) {
		var n = e.ownerDocument;
		if (e.data === "$~") e._reactRetry = t;
		else if (e.data !== "$?" || n.readyState !== "loading") t();
		else {
			var r = function() {
				t(), n.removeEventListener("DOMContentLoaded", r);
			};
			n.addEventListener("DOMContentLoaded", r), e._reactRetry = r;
		}
	}
	function lm(e) {
		for (; e != null; e = e.nextSibling) {
			var t = e.nodeType;
			if (t === 1 || t === 3) break;
			if (t === 8) {
				if (t = e.data, t === "$" || t === "$!" || t === "$?" || t === "$~" || t === "&" || t === "F!" || t === "F") break;
				if (t === "/$" || t === "/&") return null;
			}
		}
		return e;
	}
	var um = null;
	function dm(e) {
		e = e.nextSibling;
		for (var t = 0; e;) {
			if (e.nodeType === 8) {
				var n = e.data;
				if (n === "/$" || n === "/&") {
					if (t === 0) return lm(e.nextSibling);
					t--;
				} else n !== "$" && n !== "$!" && n !== "$?" && n !== "$~" && n !== "&" || t++;
			}
			e = e.nextSibling;
		}
		return null;
	}
	function fm(e) {
		e = e.previousSibling;
		for (var t = 0; e;) {
			if (e.nodeType === 8) {
				var n = e.data;
				if (n === "$" || n === "$!" || n === "$?" || n === "$~" || n === "&") {
					if (t === 0) return e;
					t--;
				} else n !== "/$" && n !== "/&" || t++;
			}
			e = e.previousSibling;
		}
		return null;
	}
	function pm(e, t) {
		function n() {
			r = !0;
		}
		if (e.ownerDocument.activeElement === e) return !0;
		var r = !1;
		try {
			e.ownerDocument.addEventListener("focus", n, !0), (e.focus || HTMLElement.prototype.focus).call(e, t);
		} finally {
			e.ownerDocument.removeEventListener("focus", n, !0);
		}
		return r;
	}
	function mm(e) {
		yp(function() {
			yp(function(t) {
				return e(t);
			});
		});
	}
	function hm(e, t, n) {
		switch (t = lp(n), e) {
			case "html":
				if (e = t.documentElement, !e) throw Error(i(452));
				return e;
			case "head":
				if (e = t.head, !e) throw Error(i(453));
				return e;
			case "body":
				if (e = t.body, !e) throw Error(i(454));
				return e;
			default: throw Error(i(451));
		}
	}
	function gm(e, t, n) {
		for (var r in n) {
			var i = n[r];
			n.hasOwnProperty(r) && i != null && $(e, t, r, null, rp, i);
		}
		n.dangerouslySetInnerHTML != null && (e.textContent = ""), e.onclick === wn && (e.onclick = null), Lt(e);
	}
	function _m(e) {
		for (var t = e.attributes; t.length;) e.removeAttributeNode(t[0]);
		Lt(e);
	}
	var vm = /* @__PURE__ */ new Map(), ym = /* @__PURE__ */ new Set();
	function bm(e) {
		if (typeof e.getRootNode == "function") {
			var t = e.getRootNode();
			if (t.nodeType === 9 || t.nodeType === 11) return t;
		}
		return e.nodeType === 9 ? e : e.ownerDocument;
	}
	var xm = A.d;
	A.d = {
		f: Sm,
		r: Cm,
		D: Em,
		C: Dm,
		L: Om,
		m: km,
		X: jm,
		S: Am,
		M: Mm
	};
	function Sm() {
		var e = xm.f(), t = zd();
		return e || t;
	}
	function Cm(e) {
		var t = zt(e);
		t !== null && t.tag === 5 && t.type === "form" ? ac(t) : xm.r(e);
	}
	var wm = typeof document > "u" ? null : document;
	function Tm(e, t, n) {
		var r = wm;
		if (r && typeof t == "string" && t) {
			var i = ln(t);
			i = "link[rel=\"" + e + "\"][href=\"" + i + "\"]", typeof n == "string" && (i += "[crossorigin=\"" + n + "\"]"), ym.has(i) || (ym.add(i), e = {
				rel: e,
				crossOrigin: n,
				href: t
			}, r.querySelector(i) === null && (t = r.createElement("link"), np(t, "link", e), Ht(t), r.head.appendChild(t)));
		}
	}
	function Em(e) {
		xm.D(e), Tm("dns-prefetch", e, null);
	}
	function Dm(e, t) {
		xm.C(e, t), Tm("preconnect", e, t);
	}
	function Om(e, t, n) {
		xm.L(e, t, n);
		var r = wm;
		if (r && e && t) {
			var i = "link[rel=\"preload\"][as=\"" + ln(t) + "\"]";
			t === "image" && n && n.imageSrcSet ? (i += "[imagesrcset=\"" + ln(n.imageSrcSet) + "\"]", typeof n.imageSizes == "string" && (i += "[imagesizes=\"" + ln(n.imageSizes) + "\"]")) : i += "[href=\"" + ln(e) + "\"]";
			var a = i;
			switch (t) {
				case "style":
					a = Pm(e);
					break;
				case "script": a = Rm(e);
			}
			if (!(vm.has(a) || (e = E({
				rel: "preload",
				href: t === "image" && n && n.imageSrcSet ? void 0 : e,
				as: t
			}, n), vm.set(a, e), r.querySelector(i) !== null || t === "style" && r.querySelector(Fm(a)) || t === "script" && r.querySelector(zm(a))))) {
				var o = r.createElement("link");
				np(o, "link", e), t === "style" && (o[It] = !0, o.onload = o.onerror = function() {
					Ut(o);
				}), Ht(o), r.head.appendChild(o);
			}
		}
	}
	function km(e, t) {
		xm.m(e, t);
		var n = wm;
		if (n && e) {
			var r = t && typeof t.as == "string" ? t.as : "script", i = "link[rel=\"modulepreload\"][as=\"" + ln(r) + "\"][href=\"" + ln(e) + "\"]", a = i;
			switch (r) {
				case "audioworklet":
				case "paintworklet":
				case "serviceworker":
				case "sharedworker":
				case "worker":
				case "script": a = Rm(e);
			}
			if (!vm.has(a) && (e = E({
				rel: "modulepreload",
				href: e
			}, t), vm.set(a, e), n.querySelector(i) === null)) {
				switch (r) {
					case "audioworklet":
					case "paintworklet":
					case "serviceworker":
					case "sharedworker":
					case "worker":
					case "script": if (n.querySelector(zm(a))) return;
				}
				r = n.createElement("link"), np(r, "link", e), Ht(r), n.head.appendChild(r);
			}
		}
	}
	function Am(e, t, n) {
		xm.S(e, t, n);
		var r = wm;
		if (r && e) {
			var i = Vt(r).hoistableStyles, a = Pm(e);
			t ||= "default";
			var o = i.get(a);
			if (!o) {
				var s = {
					loading: 0,
					preload: null
				};
				if (o = r.querySelector(Fm(a))) s.loading = 5;
				else {
					e = E({
						rel: "stylesheet",
						href: e,
						"data-precedence": t
					}, n), (n = vm.get(a)) && Hm(e, n);
					var c = o = r.createElement("link");
					Ht(c), np(c, "link", e), c._p = new Promise(function(e, t) {
						c.onload = e, c.onerror = t;
					}), c.addEventListener("load", function() {
						s.loading |= 1;
					}), c.addEventListener("error", function() {
						s.loading |= 2;
					}), s.loading |= 4, Vm(o, t, r);
				}
				o = {
					type: "stylesheet",
					instance: o,
					count: 1,
					state: s
				}, i.set(a, o);
			}
		}
	}
	function jm(e, t) {
		xm.X(e, t);
		var n = wm;
		if (n && e) {
			var r = Vt(n).hoistableScripts, i = Rm(e), a = r.get(i);
			a || (a = n.querySelector(zm(i)), a || (e = E({
				src: e,
				async: !0
			}, t), (t = vm.get(i)) && Um(e, t), a = n.createElement("script"), Ht(a), np(a, "link", e), n.head.appendChild(a)), a = {
				type: "script",
				instance: a,
				count: 1,
				state: null
			}, r.set(i, a));
		}
	}
	function Mm(e, t) {
		xm.M(e, t);
		var n = wm;
		if (n && e) {
			var r = Vt(n).hoistableScripts, i = Rm(e), a = r.get(i);
			a || (a = n.querySelector(zm(i)), a || (e = E({
				src: e,
				async: !0,
				type: "module"
			}, t), (t = vm.get(i)) && Um(e, t), a = n.createElement("script"), Ht(a), np(a, "link", e), n.head.appendChild(a)), a = {
				type: "script",
				instance: a,
				count: 1,
				state: null
			}, r.set(i, a));
		}
	}
	function Nm(e, t, n, r) {
		var a = (a = ke.current) ? bm(a) : null;
		if (!a) throw Error(i(446));
		switch (e) {
			case "meta":
			case "title": return null;
			case "style": return typeof n.precedence == "string" && typeof n.href == "string" ? (n = Pm(n.href), t = Vt(a).hoistableStyles, r = t.get(n), r || (r = {
				type: "style",
				instance: null,
				count: 0,
				state: null
			}, t.set(n, r)), r) : {
				type: "void",
				instance: null,
				count: 0,
				state: null
			};
			case "link":
				if (n.rel === "stylesheet" && typeof n.href == "string" && typeof n.precedence == "string") {
					e = Pm(n.href);
					var o = Vt(a).hoistableStyles, s = o.get(e);
					if (s || (a = a.ownerDocument || a, s = {
						type: "stylesheet",
						instance: null,
						count: 0,
						state: {
							loading: 0,
							preload: null
						}
					}, o.set(e, s), (o = a.querySelector(Fm(e))) ? o._p || (s.instance = o, s.state.loading = 5) : (o = vm.get(e), o || (o = {
						rel: "preload",
						as: "style",
						href: n.href,
						crossOrigin: n.crossOrigin,
						integrity: n.integrity,
						media: n.media,
						hrefLang: n.hrefLang,
						referrerPolicy: n.referrerPolicy
					}, vm.set(e, o)), Lm(a, e, o, s.state))), t && r === null) throw Error(i(528, ""));
					return s;
				}
				if (t && r !== null) throw Error(i(529, ""));
				return null;
			case "script": return t = n.async, n = n.src, typeof n == "string" && t && typeof t != "function" && typeof t != "symbol" ? (n = Rm(n), t = Vt(a).hoistableScripts, r = t.get(n), r || (r = {
				type: "script",
				instance: null,
				count: 0,
				state: null
			}, t.set(n, r)), r) : {
				type: "void",
				instance: null,
				count: 0,
				state: null
			};
			default: throw Error(i(444, e));
		}
	}
	function Pm(e) {
		return "href=\"" + ln(e) + "\"";
	}
	function Fm(e) {
		return "link[rel=\"stylesheet\"][" + e + "]";
	}
	function Im(e) {
		return E({}, e, {
			"data-precedence": e.precedence,
			precedence: null
		});
	}
	function Lm(e, t, n, r) {
		if (t = e.querySelector("link[rel=\"preload\"][as=\"style\"][" + t + "]")) {
			if (!0 !== t[It]) {
				r.loading = 1;
				return;
			}
		} else t = e.createElement("link"), t[It] = !0, t.onload = t.onerror = Ut.bind(null, t), np(t, "link", n), Ht(t), e.head.appendChild(t);
		r.preload = t, t.addEventListener("load", function() {
			return r.loading |= 1;
		}), t.addEventListener("error", function() {
			return r.loading |= 2;
		});
	}
	function Rm(e) {
		return "[src=\"" + ln(e) + "\"]";
	}
	function zm(e) {
		return "script[async]" + e;
	}
	function Bm(e, t, n) {
		if (t.count++, t.instance === null) switch (t.type) {
			case "style":
				var r = e.querySelector("style[data-href~=\"" + ln(n.href) + "\"]");
				if (r) return t.instance = r, Ht(r), r;
				var a = E({}, n, {
					"data-href": n.href,
					"data-precedence": n.precedence,
					href: null,
					precedence: null
				});
				return r = (e.ownerDocument || e).createElement("style"), Ht(r), np(r, "style", a), Vm(r, n.precedence, e), t.instance = r;
			case "stylesheet":
				a = Pm(n.href);
				var o = e.querySelector(Fm(a));
				if (o) return t.state.loading |= 4, t.instance = o, Ht(o), o;
				r = Im(n), (a = vm.get(a)) && Hm(r, a), o = (e.ownerDocument || e).createElement("link"), Ht(o);
				var s = o;
				return s._p = new Promise(function(e, t) {
					s.onload = e, s.onerror = t;
				}), np(o, "link", r), t.state.loading |= 4, Vm(o, n.precedence, e), t.instance = o;
			case "script": return o = Rm(n.src), (a = e.querySelector(zm(o))) ? (t.instance = a, Ht(a), a) : (r = n, (a = vm.get(o)) && (r = E({}, n), Um(r, a)), e = e.ownerDocument || e, a = e.createElement("script"), Ht(a), np(a, "link", r), e.head.appendChild(a), t.instance = a);
			case "void": return null;
			default: throw Error(i(443, t.type));
		}
		else t.type === "stylesheet" && !(t.state.loading & 4) && (r = t.instance, t.state.loading |= 4, Vm(r, n.precedence, e));
		return t.instance;
	}
	function Vm(e, t, n) {
		for (var r = n.querySelectorAll("link[rel=\"stylesheet\"][data-precedence],style[data-precedence]"), i = r.length ? r[r.length - 1] : null, a = i, o = 0; o < r.length; o++) {
			var s = r[o];
			if (s.dataset.precedence === t) a = s;
			else if (a !== i) break;
		}
		a ? a.parentNode.insertBefore(e, a.nextSibling) : (t = n.nodeType === 9 ? n.head : n, t.insertBefore(e, t.firstChild));
	}
	function Hm(e, t) {
		e.crossOrigin ??= t.crossOrigin, e.referrerPolicy ??= t.referrerPolicy, e.title ??= t.title;
	}
	function Um(e, t) {
		e.crossOrigin ??= t.crossOrigin, e.referrerPolicy ??= t.referrerPolicy, e.integrity ??= t.integrity;
	}
	var Wm = null;
	function Gm(e, t, n) {
		if (Wm === null) {
			var r = /* @__PURE__ */ new Map(), i = Wm = /* @__PURE__ */ new Map();
			i.set(n, r);
		} else i = Wm, r = i.get(n), r || (r = /* @__PURE__ */ new Map(), i.set(n, r));
		if (r.has(e)) return r;
		for (r.set(e, null), n = n.getElementsByTagName(e), i = 0; i < n.length; i++) {
			var a = n[i];
			if (!(a[Ft] || a[Ot] || e === "link" && a.getAttribute("rel") === "stylesheet") && a.namespaceURI !== "http://www.w3.org/2000/svg") {
				var o = a.getAttribute(t) || "";
				o = e + o;
				var s = r.get(o);
				s ? s.push(a) : r.set(o, [a]);
			}
		}
		return r;
	}
	function Km(e, t, n) {
		e = e.ownerDocument || e, e.head.insertBefore(n, t === "title" ? e.querySelector("head > title") : null);
	}
	function qm(e, t, n) {
		if (n === 1 || t.itemProp != null) return !1;
		switch (e) {
			case "meta":
			case "title": return !0;
			case "style":
				if (typeof t.precedence != "string" || typeof t.href != "string" || t.href === "") break;
				return !0;
			case "link":
				if (typeof t.rel != "string" || typeof t.href != "string" || t.href === "" || t.onLoad || t.onError) break;
				switch (t.rel) {
					case "stylesheet": return e = t.disabled, typeof t.precedence == "string" && e == null;
					default: return !0;
				}
			case "script": if (t.async && typeof t.async != "function" && typeof t.async != "symbol" && !t.onLoad && !t.onError && t.src && typeof t.src == "string") return !0;
		}
		return !1;
	}
	function Jm(e, t) {
		return e === "img" && t.src != null && t.src !== "" && t.onLoad == null && t.loading !== "lazy";
	}
	function Ym(e) {
		return !(e.type === "stylesheet" && !(e.state.loading & 3));
	}
	function Xm(e) {
		return (e.width || 100) * (e.height || 100) * (typeof devicePixelRatio == "number" ? devicePixelRatio : 1) * .25;
	}
	function Zm(e, t) {
		typeof t.decode == "function" && (e.imgCount++, t.complete || (e.imgBytes += Xm(t), e.suspenseyImages.push(t)), e = rh.bind(e), t.decode().then(e, e));
	}
	function Qm(e, t, n, r) {
		if (n.type === "stylesheet" && (typeof r.media != "string" || !1 !== matchMedia(r.media).matches) && !(n.state.loading & 4)) {
			if (n.instance === null) {
				var i = Pm(r.href), a = t.querySelector(Fm(i));
				if (a) {
					t = a._p, typeof t == "object" && t && typeof t.then == "function" && (e.count++, e = nh.bind(e), t.then(e, e)), n.state.loading |= 4, n.instance = a, Ht(a);
					return;
				}
				a = t.ownerDocument || t, r = Im(r), (i = vm.get(i)) && Hm(r, i), a = a.createElement("link"), Ht(a);
				var o = a;
				o._p = new Promise(function(e, t) {
					o.onload = e, o.onerror = t;
				}), np(a, "link", r), n.instance = a;
			}
			e.stylesheets === null && (e.stylesheets = /* @__PURE__ */ new Map()), e.stylesheets.set(n, t), (t = n.state.preload) && !(n.state.loading & 3) && (e.count++, n = nh.bind(e), t.addEventListener("load", n), t.addEventListener("error", n));
		}
	}
	var $m = 0;
	function eh(e, t) {
		return e.stylesheets && e.count === 0 && ah(e, e.stylesheets), 0 < e.count || 0 < e.imgCount ? function(n) {
			var r = setTimeout(function() {
				if (e.stylesheets && ah(e, e.stylesheets), e.unsuspend) {
					var t = e.unsuspend;
					e.unsuspend = null, t();
				}
			}, 6e4 + t);
			0 < e.imgBytes && $m === 0 && ($m = 62500 * op());
			var i = setTimeout(function() {
				if (e.waitingForImages = !1, e.count === 0 && (e.stylesheets && ah(e, e.stylesheets), e.unsuspend)) {
					var t = e.unsuspend;
					e.unsuspend = null, t();
				}
			}, (e.imgBytes > $m ? 50 : 800) + t);
			return e.unsuspend = n, function() {
				e.unsuspend = null, clearTimeout(r), clearTimeout(i);
			};
		} : null;
	}
	function th(e) {
		if (e.count === 0 && (e.imgCount === 0 || !e.waitingForImages)) {
			if (e.stylesheets) ah(e, e.stylesheets);
			else if (e.unsuspend) {
				var t = e.unsuspend;
				e.unsuspend = null, t();
			}
		}
	}
	function nh() {
		this.count--, th(this);
	}
	function rh() {
		this.imgCount--, th(this);
	}
	var ih = null;
	function ah(e, t) {
		e.stylesheets = null, e.unsuspend !== null && (e.count++, ih = /* @__PURE__ */ new Map(), t.forEach(oh, e), ih = null, nh.call(e));
	}
	function oh(e, t) {
		if (!(t.state.loading & 4)) {
			var n = ih.get(e);
			if (n) var r = n.get(null);
			else {
				n = /* @__PURE__ */ new Map(), ih.set(e, n);
				for (var i = e.querySelectorAll("link[data-precedence],style[data-precedence]"), a = 0; a < i.length; a++) {
					var o = i[a];
					(o.nodeName === "LINK" || o.getAttribute("media") !== "not all") && (n.set(o.dataset.precedence, o), r = o);
				}
				r && n.set(null, r);
			}
			i = t.instance, o = i.getAttribute("data-precedence"), a = n.get(o) || r, a === r && n.set(null, i), n.set(o, i), this.count++, r = nh.bind(this), i.addEventListener("load", r), i.addEventListener("error", r), a ? a.parentNode.insertBefore(i, a.nextSibling) : (e = e.nodeType === 9 ? e.head : e, e.insertBefore(i, e.firstChild)), t.state.loading |= 4;
		}
	}
	var sh = {
		$$typeof: se,
		Provider: null,
		Consumer: null,
		_currentValue: Se,
		_currentValue2: Se,
		_threadCount: 0
	};
	function ch(e, t, n, r, i, a, o, s, c) {
		this.tag = 1, this.containerInfo = e, this.pingCache = this.current = this.pendingChildren = null, this.timeoutHandle = -1, this.callbackNode = this.next = this.pendingContext = this.context = this.cancelPendingCommit = null, this.callbackPriority = 0, this.expirationTimes = vt(-1), this.entangledLanes = this.shellSuspendCounter = this.errorRecoveryDisabledLanes = this.expiredLanes = this.warmLanes = this.pingedLanes = this.suspendedLanes = this.pendingLanes = 0, this.entanglements = vt(0), this.hiddenUpdates = vt(null), this.identifierPrefix = r, this.onUncaughtError = i, this.onCaughtError = a, this.onRecoverableError = o, this.pooledCache = null, this.pooledCacheLanes = 0, this.formState = c, this.transitionTypes = null, this.incompleteTransitions = /* @__PURE__ */ new Map();
	}
	function lh(e, t, n, r, i, a, o, s, c, l, u, d) {
		return e = new ch(e, t, n, o, c, l, u, d, s), t = 1, !0 === a && (t |= 24), a = Ri(3, null, null, t), e.current = a, a.stateNode = e, t = La(), t.refCount++, e.pooledCache = t, t.refCount++, a.memoizedState = {
			element: r,
			isDehydrated: n,
			cache: t
		}, bo(a), e;
	}
	function uh(e) {
		return e ? (e = Ii, e) : Ii;
	}
	function dh(e, t, n, r, i, a) {
		i = uh(i), r.context === null ? r.context = i : r.pendingContext = i, r = So(t), r.payload = { element: n }, a = a === void 0 ? null : a, a !== null && (r.callback = a), n = Co(e, r, t), n !== null && (Pd(n, e, t), wo(n, e, t));
	}
	function fh(e, t) {
		if (e = e.memoizedState, e !== null && e.dehydrated !== null) {
			var n = e.retryLane;
			e.retryLane = n !== 0 && n < t ? n : t;
		}
	}
	function ph(e, t) {
		fh(e, t), (e = e.alternate) && fh(e, t);
	}
	function mh(e) {
		if (e.tag === 13 || e.tag === 31) {
			var t = Ni(e, 67108864);
			t !== null && Pd(t, e, 67108864), ph(e, 67108864);
		}
	}
	function hh(e) {
		if (e.tag === 13 || e.tag === 31) {
			var t = jd();
			t = Ct(t);
			var n = Ni(e, t);
			n !== null && Pd(n, e, t), ph(e, t);
		}
	}
	var gh = !0;
	function _h(e, t, n, r) {
		var i = k.T;
		k.T = null;
		var a = A.p;
		try {
			A.p = 2, yh(e, t, n, r);
		} finally {
			A.p = a, k.T = i;
		}
	}
	function vh(e, t, n, r) {
		var i = k.T;
		k.T = null;
		var a = A.p;
		try {
			A.p = 8, yh(e, t, n, r);
		} finally {
			A.p = a, k.T = i;
		}
	}
	function yh(e, t, n, r) {
		if (gh) {
			var i = bh(r);
			if (i === null) Kf(e, t, r, xh, n), Mh(e, r);
			else if (Ph(i, e, t, n, r)) r.stopPropagation();
			else if (Mh(e, r), t & 4 && -1 < jh.indexOf(e)) {
				for (; i !== null;) {
					var a = zt(i);
					if (a !== null) switch (a.tag) {
						case 3:
							if (a = a.stateNode, a.current.memoizedState.isDehydrated) {
								var o = ft(a.pendingLanes);
								if (o !== 0) {
									var s = a;
									for (s.pendingLanes |= 2, s.entangledLanes |= 2; o;) {
										var c = 1 << 31 - at(o);
										s.entanglements[1] |= c, o &= ~c;
									}
									Ef(a), !(W & 6) && (_d = qe() + 500, Df(0, !1));
								}
							}
							break;
						case 31:
						case 13: s = Ni(a, 2), s !== null && Pd(s, a, 2), zd(), ph(a, 2);
					}
					if (a = bh(r), a === null && Kf(e, t, r, xh, n), a === i) break;
					i = a;
				}
				i !== null && r.stopPropagation();
			} else Kf(e, t, r, null, n);
		}
	}
	function bh(e) {
		return e = En(e), Sh(e);
	}
	var xh = null;
	function Sh(e) {
		if (xh = null, e = Rt(e), e !== null) {
			var t = o(e);
			if (t === null) e = null;
			else {
				var n = t.tag;
				if (n === 13) {
					if (e = s(t), e !== null) return e;
					e = null;
				} else if (n === 31) {
					if (e = c(t), e !== null) return e;
					e = null;
				} else if (n === 3) {
					if (t.stateNode.current.memoizedState.isDehydrated) return t.tag === 3 ? t.stateNode.containerInfo : null;
					e = null;
				} else t !== e && (e = null);
			}
		}
		return xh = e, null;
	}
	function Ch(e) {
		switch (e) {
			case "beforetoggle":
			case "cancel":
			case "click":
			case "close":
			case "contextmenu":
			case "copy":
			case "cut":
			case "auxclick":
			case "dblclick":
			case "dragend":
			case "dragstart":
			case "drop":
			case "focusin":
			case "focusout":
			case "input":
			case "invalid":
			case "keydown":
			case "keypress":
			case "keyup":
			case "mousedown":
			case "mouseup":
			case "paste":
			case "pause":
			case "play":
			case "pointercancel":
			case "pointerdown":
			case "pointerup":
			case "ratechange":
			case "reset":
			case "seeked":
			case "submit":
			case "toggle":
			case "touchcancel":
			case "touchend":
			case "touchstart":
			case "volumechange":
			case "change":
			case "selectionchange":
			case "textInput":
			case "compositionstart":
			case "compositionend":
			case "compositionupdate":
			case "beforeblur":
			case "afterblur":
			case "beforeinput":
			case "blur":
			case "fullscreenchange":
			case "fullscreenerror":
			case "focus":
			case "hashchange":
			case "popstate":
			case "select":
			case "selectstart": return 2;
			case "drag":
			case "dragenter":
			case "dragexit":
			case "dragleave":
			case "dragover":
			case "mousemove":
			case "mouseout":
			case "mouseover":
			case "pointermove":
			case "pointerout":
			case "pointerover":
			case "resize":
			case "scroll":
			case "touchmove":
			case "wheel":
			case "mouseenter":
			case "mouseleave":
			case "pointerenter":
			case "pointerleave": return 8;
			case "message": switch (Je()) {
				case Ye: return 2;
				case Xe: return 8;
				case Ze:
				case Qe: return 32;
				case $e: return 268435456;
				default: return 32;
			}
			default: return 32;
		}
	}
	var wh = !1, Th = null, Eh = null, Dh = null, Oh = /* @__PURE__ */ new Map(), kh = /* @__PURE__ */ new Map(), Ah = [], jh = "mousedown mouseup touchcancel touchend touchstart auxclick dblclick pointercancel pointerdown pointerup dragend dragstart drop compositionend compositionstart keydown keypress keyup input textInput copy cut paste click change contextmenu reset".split(" ");
	function Mh(e, t) {
		switch (e) {
			case "focusin":
			case "focusout":
				Th = null;
				break;
			case "dragenter":
			case "dragleave":
				Eh = null;
				break;
			case "mouseover":
			case "mouseout":
				Dh = null;
				break;
			case "pointerover":
			case "pointerout":
				Oh.delete(t.pointerId);
				break;
			case "gotpointercapture":
			case "lostpointercapture": kh.delete(t.pointerId);
		}
	}
	function Nh(e, t, n, r, i, a) {
		return e === null || e.nativeEvent !== a ? (e = {
			blockedOn: t,
			domEventName: n,
			eventSystemFlags: r,
			nativeEvent: a,
			targetContainers: [i]
		}, t !== null && (t = zt(t), t !== null && mh(t)), e) : (e.eventSystemFlags |= r, t = e.targetContainers, i !== null && t.indexOf(i) === -1 && t.push(i), e);
	}
	function Ph(e, t, n, r, i) {
		switch (t) {
			case "focusin": return Th = Nh(Th, e, t, n, r, i), !0;
			case "dragenter": return Eh = Nh(Eh, e, t, n, r, i), !0;
			case "mouseover": return Dh = Nh(Dh, e, t, n, r, i), !0;
			case "pointerover":
				var a = i.pointerId;
				return Oh.set(a, Nh(Oh.get(a) || null, e, t, n, r, i)), !0;
			case "gotpointercapture": return a = i.pointerId, kh.set(a, Nh(kh.get(a) || null, e, t, n, r, i)), !0;
		}
		return !1;
	}
	function Fh(e) {
		var t = Rt(e.target);
		if (t !== null) {
			var n = o(t);
			if (n !== null) {
				if (t = n.tag, t === 13) {
					if (t = s(n), t !== null) {
						e.blockedOn = t, Et(e.priority, function() {
							hh(n);
						});
						return;
					}
				} else if (t === 31) {
					if (t = c(n), t !== null) {
						e.blockedOn = t, Et(e.priority, function() {
							hh(n);
						});
						return;
					}
				} else if (t === 3 && n.stateNode.current.memoizedState.isDehydrated) {
					e.blockedOn = n.tag === 3 ? n.stateNode.containerInfo : null;
					return;
				}
			}
		}
		e.blockedOn = null;
	}
	function Ih(e) {
		if (e.blockedOn !== null) return !1;
		for (var t = e.targetContainers; 0 < t.length;) {
			var n = bh(e.nativeEvent);
			if (n === null) {
				n = e.nativeEvent;
				var r = new n.constructor(n.type, n);
				Tn = r, n.target.dispatchEvent(r), Tn = null;
			} else return t = zt(n), t !== null && mh(t), e.blockedOn = n, !1;
			t.shift();
		}
		return !0;
	}
	function Lh(e, t, n) {
		Ih(e) && n.delete(t);
	}
	function Rh() {
		wh = !1, Th !== null && Ih(Th) && (Th = null), Eh !== null && Ih(Eh) && (Eh = null), Dh !== null && Ih(Dh) && (Dh = null), Oh.forEach(Lh), kh.forEach(Lh);
	}
	function zh(e, n) {
		e.blockedOn === n && (e.blockedOn = null, wh || (wh = !0, t.unstable_scheduleCallback(t.unstable_NormalPriority, Rh)));
	}
	var Bh = null;
	function Vh(e) {
		Bh !== e && (Bh = e, t.unstable_scheduleCallback(t.unstable_NormalPriority, function() {
			Bh === e && (Bh = null);
			for (var t = 0; t < e.length; t += 3) {
				var n = e[t], r = e[t + 1], i = e[t + 2];
				if (typeof r != "function") {
					if (Sh(r || n) === null) continue;
					break;
				}
				var a = zt(n);
				a !== null && (e.splice(t, 3), t -= 3, rc(a, {
					pending: !0,
					data: i,
					method: n.method,
					action: r
				}, r, i));
			}
		}));
	}
	function Hh(e) {
		function t(t) {
			return zh(t, e);
		}
		Th !== null && zh(Th, e), Eh !== null && zh(Eh, e), Dh !== null && zh(Dh, e), Oh.forEach(t), kh.forEach(t);
		for (var n = 0; n < Ah.length; n++) {
			var r = Ah[n];
			r.blockedOn === e && (r.blockedOn = null);
		}
		for (; 0 < Ah.length && (n = Ah[0], n.blockedOn === null);) Fh(n), n.blockedOn === null && Ah.shift();
		if (n = (e.ownerDocument || e).$$reactFormReplay, n != null) for (r = 0; r < n.length; r += 3) {
			var i = n[r], a = n[r + 1], o = i[kt] || null;
			if (typeof a == "function") o || Vh(n);
			else if (o) {
				var s = null;
				if (a && a.hasAttribute("formAction")) {
					if (i = a, o = a[kt] || null) s = o.formAction;
					else if (Sh(i) !== null) continue;
				} else s = o.action;
				typeof s == "function" ? n[r + 1] = s : (n.splice(r, 3), r -= 3), Vh(n);
			}
		}
	}
	function Uh() {
		function e(e) {
			e.canIntercept && e.info === "react-transition" && e.intercept({
				handler: function() {
					return new Promise(function(e) {
						return i = e;
					});
				},
				focusReset: "manual",
				scroll: "manual"
			});
		}
		function t() {
			i !== null && (i(), i = null), r || setTimeout(n, 20);
		}
		function n() {
			if (!r && !navigation.transition) {
				var e = navigation.currentEntry;
				e && e.url != null && navigation.navigate(e.url, {
					state: e.getState(),
					info: "react-transition",
					history: "replace"
				});
			}
		}
		if (typeof navigation == "object") {
			var r = !1, i = null;
			return navigation.addEventListener("navigate", e), navigation.addEventListener("navigatesuccess", t), navigation.addEventListener("navigateerror", t), setTimeout(n, 100), function() {
				r = !0, navigation.removeEventListener("navigate", e), navigation.removeEventListener("navigatesuccess", t), navigation.removeEventListener("navigateerror", t), i !== null && (i(), i = null);
			};
		}
	}
	function Wh(e) {
		this._internalRoot = e;
	}
	Gh.prototype.render = Wh.prototype.render = function(e) {
		var t = this._internalRoot;
		if (t === null) throw Error(i(409));
		var n = t.current;
		dh(n, jd(), e, t, null, null);
	}, Gh.prototype.unmount = Wh.prototype.unmount = function() {
		var e = this._internalRoot;
		if (e !== null) {
			this._internalRoot = null;
			var t = e.containerInfo;
			dh(e.current, 2, null, e, null, null), zd(), t[At] = null;
		}
	};
	function Gh(e) {
		this._internalRoot = e;
	}
	Gh.prototype.unstable_scheduleHydration = function(e) {
		if (e) {
			var t = Tt();
			e = {
				blockedOn: null,
				target: e,
				priority: t
			};
			for (var n = 0; n < Ah.length && t !== 0 && t < Ah[n].priority; n++);
			Ah.splice(n, 0, e), n === 0 && Fh(e);
		}
	};
	var Kh = n.version;
	if (Kh !== "19.3.1") throw Error(i(527, Kh, "19.3.1"));
	A.findDOMNode = function(e) {
		var t = e._reactInternals;
		if (t === void 0) throw typeof e.render == "function" ? Error(i(188)) : (e = Object.keys(e).join(","), Error(i(268, e)));
		return e = d(t), e = e === null ? null : p(e), e = e === null ? null : e.stateNode, e;
	};
	var qh = {
		bundleType: 0,
		version: "19.3.1",
		rendererPackageName: "react-dom",
		currentDispatcherRef: k,
		reconcilerVersion: "19.3.1"
	};
	if (typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ < "u") {
		var Jh = __REACT_DEVTOOLS_GLOBAL_HOOK__;
		if (!Jh.isDisabled && Jh.supportsFiber) try {
			nt = Jh.inject(qh), rt = Jh;
		} catch {}
	}
	e.createRoot = function(e, t) {
		if (!a(e)) throw Error(i(299));
		var n = !1, r = "", o = Ec, s = Dc, c = Oc;
		return t != null && (!0 === t.unstable_strictMode && (n = !0), t.identifierPrefix !== void 0 && (r = t.identifierPrefix), t.onUncaughtError !== void 0 && (o = t.onUncaughtError), t.onCaughtError !== void 0 && (s = t.onCaughtError), t.onRecoverableError !== void 0 && (c = t.onRecoverableError)), t = lh(e, 1, !1, null, null, n, r, null, o, s, c, Uh), e[At] = t.current, Wf(e), new Wh(t);
	};
})), g = /* @__PURE__ */ o(((e, t) => {
	function n() {
		if (typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ < "u" && typeof __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE == "function") try {
			__REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE(n);
		} catch (e) {
			console.error(e);
		}
	}
	n(), t.exports = h();
})), _ = /* @__PURE__ */ c(u(), 1), v = Object.defineProperty, y = (e, t) => v(e, "name", {
	value: t,
	configurable: !0
});
function b(e, t) {
	if (typeof e == "function") return e(t);
	e != null && (e.current = t);
}
y(b, "setRef");
function x(...e) {
	return (t) => {
		let n = !1, r = e.map((e) => {
			let r = b(e, t);
			return !n && typeof r == "function" && (n = !0), r;
		});
		if (n) return () => {
			for (let t = 0; t < r.length; t++) {
				let n = r[t];
				typeof n == "function" ? n() : b(e[t], null);
			}
		};
	};
}
y(x, "composeRefs");
function S(...e) {
	return _.useCallback(x(...e), e);
}
y(S, "useComposedRefs");
// node_modules/@radix-ui/react-slot/dist/index.mjs
var C = Object.defineProperty, w = (e, t) => C(e, "name", {
	value: t,
	configurable: !0
});
// @__NO_SIDE_EFFECTS__
function T(e) {
	let t = _.forwardRef((t, n) => {
		let { children: r, ...i } = t, a = null, o = !1, s = [];
		r = ce(r), _.Children.forEach(r, (e) => {
			if (ie(e)) {
				o = !0;
				let t = e, n = "child" in t.props ? t.props.child : t.props.children;
				for (; oe(n) && typeof fe == "function";) n = fe(n._payload);
				a = ne(t, n), s.push(a?.props?.children);
			} else s.push(e);
		}), a ? a = _.cloneElement(a, void 0, s) : !o && _.Children.count(r) === 1 && _.isValidElement(r) && (a = r);
		let c = a ? D(a) : void 0, l = S(n, c);
		if (!a) {
			if (r || r === 0) throw Error(o ? de(e) : ue(e));
			return r;
		}
		let u = re(i, a.props ?? {});
		return a.type !== _.Fragment && (u.ref = n ? l : c), _.cloneElement(a, u);
	});
	return t.displayName = `${e}.Slot`, t;
}
w(T, "createSlot");
var ee = /* @__PURE__ */ T("Slot"), E = Symbol.for("radix.slottable");
// @__NO_SIDE_EFFECTS__
function te(e) {
	let t = /* @__PURE__ */ w((e) => "child" in e ? e.children(e.child) : e.children, "Slottable");
	return t.displayName = `${e}.Slottable`, t.__radixId = E, t;
}
w(te, "createSlottable");
var ne = /* @__PURE__ */ w((e, t) => "child" in e.props ? _.isValidElement(t) ? _.cloneElement(t, void 0, e.props.children(t.props.children)) : null : _.isValidElement(t) ? t : null, "getSlottableElementFromSlottable");
function re(e, t) {
	let n = { ...t };
	for (let r in t) {
		let i = e[r], a = t[r];
		/^on[A-Z]/.test(r) ? i && a ? n[r] = (...e) => {
			let t = a(...e);
			return i(...e), t;
		} : i && (n[r] = i) : r === "style" ? n[r] = {
			...i,
			...a
		} : r === "className" ? n[r] = [i, a].filter(Boolean).join(" ") : r === "aria-describedby" && (n[r] = le(a, i));
	}
	return {
		...e,
		...n
	};
}
w(re, "mergeProps");
function D(e) {
	let t = Object.getOwnPropertyDescriptor(e.props, "ref")?.get, n = t && "isReactWarning" in t && t.isReactWarning;
	return n ? e.ref : (t = Object.getOwnPropertyDescriptor(e, "ref")?.get, n = t && "isReactWarning" in t && t.isReactWarning, n ? e.props.ref : e.props.ref || e.ref);
}
w(D, "getElementRef");
function ie(e) {
	return _.isValidElement(e) && typeof e.type == "function" && "__radixId" in e.type && e.type.__radixId === E;
}
w(ie, "isSlottable");
var ae = Symbol.for("react.lazy");
function oe(e) {
	return typeof e == "object" && !!e && "$$typeof" in e && e.$$typeof === ae && "_payload" in e && se(e._payload);
}
w(oe, "isLazyComponent");
function se(e) {
	return typeof e == "object" && !!e && "then" in e;
}
w(se, "isPromiseLike");
function ce(e) {
	let t = e;
	for (; oe(t) && typeof fe == "function";) t = fe(t._payload);
	return t;
}
w(ce, "useUnwrappedLazy");
function le(...e) {
	let t = /* @__PURE__ */ new Set();
	for (let n of e) if (typeof n == "string") for (let e of String(n).trim().split(/\s+/)) e && t.add(e);
	return t.size > 0 ? Array.from(t).join(" ") : void 0;
}
w(le, "concatAriaDescribedby");
var ue = /* @__PURE__ */ w((e) => `${e} failed to slot onto its children. Expected a single React element child or \`Slottable\`.`, "createSlotError"), de = /* @__PURE__ */ w((e) => `${e} failed to slot onto its \`Slottable\`. Expected \`Slottable\` to receive a single React element child.`, "createSlottableError"), fe = _.use, pe = /* @__PURE__ */ o(((e) => {
	var t = Symbol.for("react.transitional.element");
	function n(e, n, r) {
		var i = null;
		if (r !== void 0 && (i = "" + r), n.key !== void 0 && (i = "" + n.key), "key" in n) for (var a in r = {}, n) a !== "key" && (r[a] = n[a]);
		else r = n;
		return n = r.ref, {
			$$typeof: t,
			type: e,
			key: i,
			ref: n === void 0 ? null : n,
			props: r
		};
	}
	e.jsx = n, e.jsxs = n;
})), me = /* @__PURE__ */ o(((e, t) => {
	t.exports = pe();
})), he = /* @__PURE__ */ c(m(), 1), O = me(), ge = Object.defineProperty, _e = (e, t) => ge(e, "name", {
	value: t,
	configurable: !0
}), ve = [
	"a",
	"button",
	"div",
	"form",
	"h2",
	"h3",
	"img",
	"input",
	"label",
	"li",
	"nav",
	"ol",
	"p",
	"select",
	"span",
	"svg",
	"ul"
].reduce((e, t) => {
	let n = /* @__PURE__ */ T(`Primitive.${t}`), r = _.forwardRef((e, r) => {
		let { asChild: i, ...a } = e, o = i ? n : t;
		return typeof window < "u" && (window[Symbol.for("radix-ui")] = !0), /* @__PURE__ */ (0, O.jsx)(o, {
			...a,
			ref: r
		});
	});
	return r.displayName = `Primitive.${t}`, {
		...e,
		[t]: r
	};
}, {});
function ye(e, t) {
	e && he.flushSync(() => e.dispatchEvent(t));
}
_e(ye, "dispatchDiscreteCustomEvent");
// node_modules/@radix-ui/react-visually-hidden/dist/index.mjs
var be = Object.defineProperty, xe = (e, t) => be(e, "name", {
	value: t,
	configurable: !0
}), k = Object.freeze({
	position: "absolute",
	border: 0,
	width: 1,
	height: 1,
	padding: 0,
	margin: -1,
	overflow: "hidden",
	clip: "rect(0, 0, 0, 0)",
	whiteSpace: "nowrap",
	wordWrap: "normal"
}), A = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ xe(function(e, t) {
	return /* @__PURE__ */ (0, O.jsx)(ve.span, {
		...e,
		ref: t,
		style: {
			...k,
			...e.style
		}
	});
}, "VisuallyHidden")), Se = Object.defineProperty, Ce = (e, t) => Se(e, "name", {
	value: t,
	configurable: !0
});
// @__NO_SIDE_EFFECTS__
function we(e, t) {
	let n = _.createContext(t);
	n.displayName = e + "Context";
	let r = /* @__PURE__ */ Ce((e) => {
		let { children: t, ...r } = e, i = _.useMemo(() => r, Object.values(r));
		return /* @__PURE__ */ (0, O.jsx)(n.Provider, {
			value: i,
			children: t
		});
	}, "Provider");
	r.displayName = e + "Provider";
	function i(r, i = {}) {
		let { optional: a = !1 } = i, o = _.useContext(n);
		if (o) return o;
		if (t !== void 0) return t;
		if (!a) throw Error(`\`${r}\` must be used within \`${e}\``);
	}
	return Ce(i, "useContext"), [r, i];
}
Ce(we, "createContext");
// @__NO_SIDE_EFFECTS__
function Te(e, t = []) {
	let n = [];
	function r(t, r) {
		let i = _.createContext(r);
		i.displayName = t + "Context";
		let a = n.length;
		n = [...n, r];
		let o = /* @__PURE__ */ Ce((t) => {
			let { scope: n, children: r, ...o } = t, s = n?.[e]?.[a] || i, c = _.useMemo(() => o, Object.values(o));
			return /* @__PURE__ */ (0, O.jsx)(s.Provider, {
				value: c,
				children: r
			});
		}, "Provider");
		o.displayName = t + "Provider";
		function s(n, o, s = {}) {
			let { optional: c = !1 } = s, l = o?.[e]?.[a] || i, u = _.useContext(l);
			if (u) return u;
			if (r !== void 0) return r;
			if (!c) throw Error(`\`${n}\` must be used within \`${t}\``);
		}
		return Ce(s, "useContext"), [o, s];
	}
	Ce(r, "createContext");
	let i = /* @__PURE__ */ Ce(() => {
		let t = n.map((e) => _.createContext(e));
		return /* @__PURE__ */ Ce(function(n) {
			let r = n?.[e] || t;
			return _.useMemo(() => ({ [`__scope${e}`]: {
				...n,
				[e]: r
			} }), [n, r]);
		}, "useScope");
	}, "createScope");
	return i.scopeName = e, [r, Ee(i, ...t)];
}
Ce(Te, "createContextScope");
function Ee(...e) {
	let t = e[0];
	if (e.length === 1) return t;
	let n = /* @__PURE__ */ Ce(() => {
		let n = e.map((e) => ({
			useScope: e(),
			scopeName: e.scopeName
		}));
		return /* @__PURE__ */ Ce(function(e) {
			let r = n.reduce((t, { useScope: n, scopeName: r }) => {
				let i = n(e)[`__scope${r}`];
				return {
					...t,
					...i
				};
			}, {});
			return _.useMemo(() => ({ [`__scope${t.scopeName}`]: r }), [r]);
		}, "useComposedScopes");
	}, "createScope");
	return n.scopeName = t.scopeName, n;
}
Ce(Ee, "composeContextScopes");
// node_modules/@radix-ui/react-use-layout-effect/dist/index.mjs
var j = globalThis?.document ? _.useLayoutEffect : () => {}, De = Object.defineProperty, Oe = (e, t) => De(e, "name", {
	value: t,
	configurable: !0
}), ke = !!(typeof window < "u" && window.document && window.document.createElement);
function Ae(e, t, { checkForDefaultPrevented: n = !0 } = {}) {
	return /* @__PURE__ */ Oe(function(r) {
		if (e?.(r), n === !1 || !r || !r.defaultPrevented) return t?.(r);
	}, "handleEvent");
}
Oe(Ae, "composeEventHandlers");
function je(e) {
	if (!ke) throw Error("Cannot access window outside of the DOM");
	return e?.ownerDocument?.defaultView ?? window;
}
Oe(je, "getOwnerWindow");
function Me(e) {
	if (!ke) throw Error("Cannot access document outside of the DOM");
	return e?.ownerDocument ?? document;
}
Oe(Me, "getOwnerDocument");
function Ne(e, t = !1) {
	let { activeElement: n } = Me(e);
	if (!n?.nodeName) return null;
	if (Pe(n) && n.contentDocument) return Ne(n.contentDocument.body, t);
	if (t) {
		let e = n.getAttribute("aria-activedescendant");
		if (e) {
			let t = Me(n).getElementById(e);
			if (t) return t;
		}
	}
	return n;
}
Oe(Ne, "getActiveElement");
function Pe(e) {
	return e.tagName === "IFRAME";
}
Oe(Pe, "isFrame");
// node_modules/@radix-ui/react-use-effect-event/dist/index.mjs
var Fe = Object.defineProperty, Ie = (e, t) => Fe(e, "name", {
	value: t,
	configurable: !0
}), Le = _.useEffectEvent, Re = _.useInsertionEffect;
function ze(e) {
	if (typeof Le == "function") return Le(e);
	let t = _.useRef(() => {
		throw Error("Cannot call an event handler while rendering.");
	});
	return typeof Re == "function" ? Re(() => {
		t.current = e;
	}) : j(() => {
		t.current = e;
	}), _.useMemo(() => ((...e) => t.current?.(...e)), []);
}
Ie(ze, "useEffectEvent");
// node_modules/@radix-ui/react-use-controllable-state/dist/index.mjs
var Be = Object.defineProperty, Ve = (e, t) => Be(e, "name", {
	value: t,
	configurable: !0
}), He = _.useInsertionEffect || j;
function Ue({ prop: e, defaultProp: t, onChange: n = /* @__PURE__ */ Ve(() => {}, "onChange"), caller: r }) {
	let [i, a, o] = We({
		defaultProp: t,
		onChange: n
	}), s = e !== void 0;
	return [s ? e : i, _.useCallback((t) => {
		if (s) {
			let n = Ge(t) ? t(e) : t;
			n !== e && o.current?.(n);
		} else a(t);
	}, [
		s,
		e,
		a,
		o
	])];
}
Ve(Ue, "useControllableState");
function We({ defaultProp: e, onChange: t }) {
	let [n, r] = _.useState(e), i = _.useRef(n), a = _.useRef(t);
	return He(() => {
		a.current = t;
	}, [t]), _.useEffect(() => {
		i.current !== n && (a.current?.(n), i.current = n);
	}, [n, i]), [
		n,
		r,
		a
	];
}
Ve(We, "useUncontrolledState");
function Ge(e) {
	return typeof e == "function";
}
Ve(Ge, "isFunction");
var Ke = Symbol("RADIX:SYNC_STATE");
function qe(e, t, n, r) {
	let { prop: i, defaultProp: a, onChange: o, caller: s } = t, c = i !== void 0, l = ze(o), u = [{
		...n,
		state: a
	}];
	r && u.push(r);
	let [d, f] = _.useReducer((t, n) => {
		if (n.type === Ke) return {
			...t,
			state: n.state
		};
		let r = e(t, n);
		return c && !Object.is(r.state, t.state) && l(r.state), r;
	}, ...u), p = d.state, m = _.useRef(p);
	_.useEffect(() => {
		m.current !== p && (m.current = p, c || l(p));
	}, [
		p,
		m,
		c
	]);
	let h = _.useMemo(() => i === void 0 ? d : {
		...d,
		state: i
	}, [d, i]);
	return _.useEffect(() => {
		c && !Object.is(i, d.state) && f({
			type: Ke,
			state: i
		});
	}, [
		i,
		d.state,
		c
	]), [h, f];
}
Ve(qe, "useControllableStateReducer");
// node_modules/@radix-ui/react-presence/dist/index.mjs
var Je = Object.defineProperty, Ye = (e, t) => Je(e, "name", {
	value: t,
	configurable: !0
});
function Xe(e, t) {
	return _.useReducer((e, n) => t[e][n] ?? e, e);
}
Ye(Xe, "useStateMachine");
var Ze = /* @__PURE__ */ Ye((e) => {
	let { present: t, children: n } = e, r = Qe(t), i = typeof n == "function" ? n({ present: r.isPresent }) : _.Children.only(n), a = et(r.ref, nt(i));
	return typeof n == "function" || r.isPresent ? _.cloneElement(i, { ref: a }) : null;
}, "Presence");
function Qe(e) {
	let [t, n] = _.useState(), r = _.useRef(null), i = _.useRef(e), a = _.useRef("none"), o = _.useRef(void 0), [s, c] = Xe(e ? "mounted" : "unmounted", {
		mounted: {
			UNMOUNT: "unmounted",
			ANIMATION_OUT: "unmountSuspended"
		},
		unmountSuspended: {
			MOUNT: "mounted",
			ANIMATION_END: "unmounted"
		},
		unmounted: { MOUNT: "mounted" }
	});
	return _.useEffect(() => {
		s === "mounted" ? (a.current = o.current ?? tt(r.current), o.current = void 0) : a.current = "none";
	}, [s]), j(() => {
		let t = r.current, n = i.current;
		if (n !== e) {
			let r = a.current, s = tt(t);
			e ? (o.current = s, c("MOUNT")) : s === "none" || t?.display === "none" ? c("UNMOUNT") : c(n && r !== s ? "ANIMATION_OUT" : "UNMOUNT"), i.current = e;
		}
	}, [e, c]), j(() => {
		if (t) {
			let e, n = t.ownerDocument.defaultView ?? window, o = /* @__PURE__ */ Ye((a) => {
				let o = tt(r.current).includes(CSS.escape(a.animationName));
				if (a.target === t && o && (c("ANIMATION_END"), !i.current)) {
					let r = t.style.animationFillMode;
					t.style.animationFillMode = "forwards", e = n.setTimeout(() => {
						t.style.animationFillMode === "forwards" && (t.style.animationFillMode = r);
					});
				}
			}, "handleAnimationEnd"), s = /* @__PURE__ */ Ye((e) => {
				e.target === t && (a.current = tt(r.current));
			}, "handleAnimationStart");
			return t.addEventListener("animationstart", s), t.addEventListener("animationcancel", o), t.addEventListener("animationend", o), () => {
				n.clearTimeout(e), t.removeEventListener("animationstart", s), t.removeEventListener("animationcancel", o), t.removeEventListener("animationend", o);
			};
		}
		c("ANIMATION_END");
	}, [t, c]), {
		isPresent: ["mounted", "unmountSuspended"].includes(s),
		ref: _.useCallback((e) => {
			if (e) {
				let t = getComputedStyle(e);
				r.current = t, o.current = tt(t);
			} else r.current = null;
			n(e);
		}, [])
	};
}
Ye(Qe, "usePresence");
function $e(e, t) {
	if (typeof e == "function") return e(t);
	e != null && (e.current = t);
}
Ye($e, "setRef");
function et(...e) {
	let t = _.useRef(e);
	return t.current = e, _.useCallback((e) => {
		let n = t.current, r = !1, i = n.map((t) => {
			let n = $e(t, e);
			return !r && typeof n == "function" && (r = !0), n;
		});
		if (r) return () => {
			for (let e = 0; e < i.length; e++) {
				let t = i[e];
				typeof t == "function" ? t() : $e(n[e], null);
			}
		};
	}, []);
}
Ye(et, "useStableComposedRefs");
function tt(e) {
	return e?.animationName || "none";
}
Ye(tt, "getAnimationName");
function nt(e) {
	let t = Object.getOwnPropertyDescriptor(e.props, "ref")?.get, n = t && "isReactWarning" in t && t.isReactWarning;
	return n ? e.ref : (t = Object.getOwnPropertyDescriptor(e, "ref")?.get, n = t && "isReactWarning" in t && t.isReactWarning, n ? e.props.ref : e.props.ref || e.ref);
}
Ye(nt, "getElementRef");
// node_modules/@radix-ui/react-id/dist/index.mjs
var rt = Object.defineProperty, it = (e, t) => rt(e, "name", {
	value: t,
	configurable: !0
}), at = _.useId || (() => void 0), ot = 0;
function st(e) {
	let [t, n] = _.useState(at());
	return j(() => {
		e || n((e) => e ?? String(ot++));
	}, [e]), e || (t ? `radix-${t}` : "");
}
it(st, "useId");
// node_modules/@radix-ui/react-direction/dist/index.mjs
var ct = Object.defineProperty, lt = (e, t) => ct(e, "name", {
	value: t,
	configurable: !0
}), ut = {
	LTR: "ltr",
	RTL: "rtl"
}, dt = _.createContext(void 0), ft = /* @__PURE__ */ lt((e) => {
	let { dir: t, children: n } = e;
	return /* @__PURE__ */ (0, O.jsx)(dt.Provider, {
		value: t,
		children: n
	});
}, "DirectionProvider");
function pt(e) {
	let t = _.useContext(dt);
	return e || t || ut.LTR;
}
lt(pt, "useDirection");
// node_modules/@radix-ui/react-use-callback-ref/dist/index.mjs
var mt = Object.defineProperty, ht = (e, t) => mt(e, "name", {
	value: t,
	configurable: !0
}), gt = _.useInsertionEffect;
function _t(e) {
	let t = _.useRef(e);
	return typeof gt == "function" ? gt(() => {
		t.current = e;
	}) : j(() => {
		t.current = e;
	}), _.useMemo(() => ((...e) => t.current?.(...e)), []);
}
ht(_t, "useCallbackRef");
// node_modules/@radix-ui/react-dismissable-layer/dist/index.mjs
var vt = Object.defineProperty, M = (e, t) => vt(e, "name", {
	value: t,
	configurable: !0
}), yt = "dismissableLayer.update", bt = "dismissableLayer.pointerDownOutside", xt = "dismissableLayer.focusOutside", St, Ct = _.createContext({
	layers: /* @__PURE__ */ new Set(),
	layersWithOutsidePointerEventsDisabled: /* @__PURE__ */ new Set(),
	branches: /* @__PURE__ */ new Set(),
	dismissableSurfaces: /* @__PURE__ */ new Set()
}), wt = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ M(function(e, t) {
	let { disableOutsidePointerEvents: n = !1, deferPointerDownOutside: r = !1, onEscapeKeyDown: i, onPointerDownOutside: a, onFocusOutside: o, onInteractOutside: s, onDismiss: c, ...l } = e, u = _.useContext(Ct), [d, f] = _.useState(null), p = d?.ownerDocument ?? globalThis?.document, [, m] = _.useState({}), h = S(t, f), g = Array.from(u.layers), [v] = [...u.layersWithOutsidePointerEventsDisabled].slice(-1), y = v ? g.indexOf(v) : -1, b = d ? g.indexOf(d) : -1, x = u.layersWithOutsidePointerEventsDisabled.size > 0, C = b >= y, w = _.useRef(!1), T = Dt((e) => {
		a?.(e), s?.(e), e.defaultPrevented || c?.();
	}, {
		ownerDocument: p,
		deferPointerDownOutside: r,
		isDeferredPointerDownOutsideRef: w,
		dismissableSurfaces: u.dismissableSurfaces,
		shouldHandlePointerDownOutside: _.useCallback((e) => {
			if (!(e instanceof Node)) return !1;
			let t = [...u.branches].some((t) => t.contains(e));
			return C && !t;
		}, [u.branches, C])
	}), ee = Ot((e) => {
		if (r && w.current) return;
		let t = e.target;
		[...u.branches].some((e) => e.contains(t)) || (o?.(e), s?.(e), e.defaultPrevented || c?.());
	}, p), E = d ? b === g.length - 1 : !1, te = _t((e) => {
		e.key === "Escape" && (i?.(e), !e.defaultPrevented && c && (e.preventDefault(), c()));
	});
	return _.useEffect(() => {
		if (E) return p.addEventListener("keydown", te, { capture: !0 }), () => p.removeEventListener("keydown", te, { capture: !0 });
	}, [
		p,
		E,
		te
	]), _.useEffect(() => {
		if (d) return n && (u.layersWithOutsidePointerEventsDisabled.size === 0 && (St = p.body.style.pointerEvents, p.body.style.pointerEvents = "none"), u.layersWithOutsidePointerEventsDisabled.add(d)), u.layers.add(d), kt(), () => {
			n && (u.layersWithOutsidePointerEventsDisabled.delete(d), u.layersWithOutsidePointerEventsDisabled.size === 0 && (p.body.style.pointerEvents = St));
		};
	}, [
		d,
		p,
		n,
		u
	]), _.useEffect(() => () => {
		d && (u.layers.delete(d), u.layersWithOutsidePointerEventsDisabled.delete(d), kt());
	}, [d, u]), _.useEffect(() => {
		let e = /* @__PURE__ */ M(() => m({}), "handleUpdate");
		return document.addEventListener(yt, e), () => document.removeEventListener(yt, e);
	}, []), /* @__PURE__ */ (0, O.jsx)(ve.div, {
		...l,
		ref: h,
		style: {
			pointerEvents: x ? C ? "auto" : "none" : void 0,
			...e.style
		},
		onFocusCapture: Ae(e.onFocusCapture, ee.onFocusCapture),
		onBlurCapture: Ae(e.onBlurCapture, ee.onBlurCapture),
		onPointerDownCapture: Ae(e.onPointerDownCapture, T.onPointerDownCapture)
	});
}, "DismissableLayer"));
function Tt() {
	let e = _.useContext(Ct), [t, n] = _.useState(null);
	return _.useEffect(() => {
		if (t) return e.dismissableSurfaces.add(t), () => {
			e.dismissableSurfaces.delete(t);
		};
	}, [t, e.dismissableSurfaces]), n;
}
M(Tt, "useDismissableLayerSurface");
var Et = /* @__PURE__ */ M(() => !0, "IS_TRUE");
function Dt(e, t) {
	let { ownerDocument: n = globalThis?.document, deferPointerDownOutside: r = !1, isDeferredPointerDownOutsideRef: i, dismissableSurfaces: a, shouldHandlePointerDownOutside: o = Et } = t, s = _t(e), c = _.useRef(!1), l = _.useRef(!1), u = _.useRef(/* @__PURE__ */ new Map()), d = _.useRef(() => {});
	return _.useEffect(() => {
		function e() {
			l.current = !1, i.current = !1, u.current.clear();
		}
		M(e, "resetOutsideInteraction");
		function t() {
			return Array.from(u.current.values()).some(Boolean);
		}
		M(t, "isOutsideInteractionIntercepted");
		function f(e) {
			if (!l.current) return;
			let t = e.target;
			t instanceof Node && [...a].some((e) => e.contains(t)) || u.current.set(e.type, !0), e.type === "click" && window.setTimeout(() => {
				l.current && d.current();
			}, 0);
		}
		M(f, "handleInteractionCapture");
		function p(e) {
			l.current && u.current.set(e.type, !1);
		}
		M(p, "handleInteractionBubble");
		let m = /* @__PURE__ */ M((a) => {
			if (a.target && !c.current) {
				let f = function() {
					n.removeEventListener("click", d.current);
					let r = t();
					e(), r || At(bt, s, p, { discrete: !0 });
				};
				if (M(f, "handleAndDispatchPointerDownOutsideEvent"), !o(a.target)) {
					n.removeEventListener("click", d.current), e(), c.current = !1;
					return;
				}
				let p = { originalEvent: a };
				l.current = !0, i.current = r && a.button === 0, u.current.clear(), !r || a.button !== 0 ? f() : (n.removeEventListener("click", d.current), d.current = f, n.addEventListener("click", d.current, { once: !0 }));
			} else n.removeEventListener("click", d.current), e();
			c.current = !1;
		}, "handlePointerDown"), h = [
			"pointerup",
			"mousedown",
			"mouseup",
			"touchstart",
			"touchend",
			"click"
		];
		for (let e of h) n.addEventListener(e, f, !0), n.addEventListener(e, p);
		let g = window.setTimeout(() => {
			n.addEventListener("pointerdown", m);
		}, 0);
		return () => {
			window.clearTimeout(g), n.removeEventListener("pointerdown", m), n.removeEventListener("click", d.current);
			for (let e of h) n.removeEventListener(e, f, !0), n.removeEventListener(e, p);
		};
	}, [
		n,
		s,
		r,
		i,
		a,
		o
	]), { onPointerDownCapture: /* @__PURE__ */ M(() => c.current = !0, "onPointerDownCapture") };
}
M(Dt, "usePointerDownOutside");
function Ot(e, t = globalThis?.document) {
	let n = _t(e), r = _.useRef(!1);
	return _.useEffect(() => {
		let e = /* @__PURE__ */ M((e) => {
			e.target && !r.current && At(xt, n, { originalEvent: e }, { discrete: !1 });
		}, "handleFocus");
		return t.addEventListener("focusin", e), () => t.removeEventListener("focusin", e);
	}, [t, n]), {
		onFocusCapture: /* @__PURE__ */ M(() => r.current = !0, "onFocusCapture"),
		onBlurCapture: /* @__PURE__ */ M(() => r.current = !1, "onBlurCapture")
	};
}
M(Ot, "useFocusOutside");
function kt() {
	let e = new CustomEvent(yt);
	document.dispatchEvent(e);
}
M(kt, "dispatchUpdate");
function At(e, t, n, { discrete: r }) {
	let i = n.originalEvent.target, a = new CustomEvent(e, {
		bubbles: !1,
		cancelable: !0,
		detail: n
	});
	t && i.addEventListener(e, t, { once: !0 }), r ? ye(i, a) : i.dispatchEvent(a);
}
M(At, "handleAndDispatchCustomEvent");
// node_modules/@radix-ui/react-focus-scope/dist/index.mjs
var jt = Object.defineProperty, Mt = (e, t) => jt(e, "name", {
	value: t,
	configurable: !0
}), Nt = "focusScope.autoFocusOnMount", Pt = "focusScope.autoFocusOnUnmount", Ft = {
	bubbles: !1,
	cancelable: !0
}, It = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ Mt(function(e, t) {
	let { loop: n = !1, trapped: r = !1, branches: i, onMountAutoFocus: a, onUnmountAutoFocus: o, ...s } = e, [c, l] = _.useState(null), u = _t(a), d = _t(o), f = _.useRef(null), p = S(t, l), m = _.useRef(i);
	_.useEffect(() => {
		m.current = i;
	});
	let h = _.useCallback((e) => e ? c?.contains(e) ? !0 : !!m.current?.some((t) => t.contains(e)) : !1, [c]), g = _.useRef({
		paused: !1,
		pause() {
			this.paused = !0;
		},
		resume() {
			this.paused = !1;
		}
	}).current;
	_.useEffect(() => {
		if (r) {
			let e = function(e) {
				if (g.paused || !c) return;
				let t = e.target;
				h(t) ? f.current = t : qt(f.current, { select: !0 });
			}, t = function(e) {
				if (g.paused || !c) return;
				let t = e.relatedTarget;
				t !== null && (h(t) || qt(f.current, { select: !0 }));
			}, n = function(e) {
				if (document.activeElement === document.body) for (let t of e) t.removedNodes.length > 0 && qt(c);
			};
			Mt(e, "handleFocusIn"), Mt(t, "handleFocusOut"), Mt(n, "handleMutations"), document.addEventListener("focusin", e), document.addEventListener("focusout", t);
			let r = new MutationObserver(n);
			return c && r.observe(c, {
				childList: !0,
				subtree: !0
			}), () => {
				document.removeEventListener("focusin", e), document.removeEventListener("focusout", t), r.disconnect();
			};
		}
	}, [
		r,
		c,
		g.paused,
		h
	]), _.useEffect(() => {
		if (c) {
			Jt.add(g);
			let e = document.activeElement;
			if (!c.contains(e)) {
				let t = new CustomEvent(Nt, Ft);
				c.addEventListener(Nt, u), c.dispatchEvent(t), t.defaultPrevented || (Vt(Zt(Ut(c)), { select: !0 }), document.activeElement === e && qt(c));
			}
			return () => {
				c.removeEventListener(Nt, u);
				let t = c.ownerDocument, n = CustomEvent;
				setTimeout(() => {
					let r = new n(Pt, Ft);
					c.addEventListener(Pt, d), c.dispatchEvent(r), r.defaultPrevented || qt(e ?? t.body, { select: !0 }), c.removeEventListener(Pt, d), Jt.remove(g);
				}, 0);
			};
		}
	}, [
		c,
		u,
		d,
		g
	]);
	let v = _.useCallback((e) => {
		if (!n && !r || g.paused) return;
		let t = e.key === "Tab" && !e.altKey && !e.ctrlKey && !e.metaKey, i = document.activeElement;
		if (t && i) {
			let t = e.currentTarget, [r, a] = Ht(t);
			r && a ? !e.shiftKey && i === a ? (e.preventDefault(), n && qt(r, { select: !0 })) : e.shiftKey && i === r && (e.preventDefault(), n && qt(a, { select: !0 })) : i === t && e.preventDefault();
		}
	}, [
		n,
		r,
		g.paused
	]);
	return /* @__PURE__ */ (0, O.jsx)(ve.div, {
		tabIndex: -1,
		...s,
		ref: p,
		onKeyDown: v
	});
}, "FocusScope")), Lt = _.createContext(null);
function Rt({ registry: e, children: t }) {
	return e ? /* @__PURE__ */ (0, O.jsx)(Lt.Provider, {
		value: e,
		children: t
	}) : t;
}
Mt(Rt, "FocusScopeBranchProvider");
function zt() {
	let [e, t] = _.useState([]);
	return {
		nodes: e,
		registry: _.useMemo(() => ({
			add: /* @__PURE__ */ Mt((e) => t((t) => t.includes(e) ? t : [...t, e]), "add"),
			remove: /* @__PURE__ */ Mt((e) => t((t) => t.filter((t) => t !== e)), "remove")
		}), [])
	};
}
Mt(zt, "useFocusScopeBranchRegistry");
function Bt(e) {
	let t = _.useContext(Lt);
	_.useEffect(() => {
		if (e && t) return t.add(e), () => t.remove(e);
	}, [e, t]);
}
Mt(Bt, "useFocusScopeBranch");
function Vt(e, { select: t = !1 } = {}) {
	let n = document.activeElement;
	for (let r of e) if (qt(r, { select: t }), document.activeElement !== n) return;
}
Mt(Vt, "focusFirst");
function Ht(e) {
	let t = Ut(e);
	return [Wt(t, e), Wt(t.reverse(), e)];
}
Mt(Ht, "getTabbableEdges");
function Ut(e) {
	let t = [], n = e.ownerDocument.createTreeWalker(e, NodeFilter.SHOW_ELEMENT, { acceptNode: /* @__PURE__ */ Mt((e) => {
		let t = e.tagName === "INPUT" && e.type === "hidden";
		return e.disabled || e.hidden || t ? NodeFilter.FILTER_SKIP : e.tabIndex >= 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
	}, "acceptNode") });
	for (; n.nextNode();) t.push(n.currentNode);
	return t;
}
Mt(Ut, "getTabbableCandidates");
function Wt(e, t) {
	let n = typeof t.checkVisibility == "function" && t.checkVisibility({ checkVisibilityCSS: !0 });
	for (let r of e) if (!(n ? !r.checkVisibility({ checkVisibilityCSS: !0 }) : Gt(r, { upTo: t }))) return r;
}
Mt(Wt, "findVisible");
function Gt(e, { upTo: t }) {
	if (getComputedStyle(e).visibility === "hidden") return !0;
	for (; e;) {
		if (t !== void 0 && e === t) return !1;
		if (getComputedStyle(e).display === "none") return !0;
		e = e.parentElement;
	}
	return !1;
}
Mt(Gt, "isHidden");
function Kt(e) {
	return e.tagName === "INPUT" && typeof e.select == "function";
}
Mt(Kt, "isSelectableInput");
function qt(e, { select: t = !1 } = {}) {
	if (e && e.focus) {
		let n = (e.ownerDocument ?? document).activeElement;
		e.focus({ preventScroll: !0 }), e !== n && Kt(e) && t && e.select();
	}
}
Mt(qt, "focus");
var Jt = Yt();
function Yt() {
	let e = [];
	return {
		add(t) {
			let n = e[0];
			t !== n && n?.pause(), e = Xt(e, t), e.unshift(t);
		},
		remove(t) {
			e = Xt(e, t), e[0]?.resume();
		}
	};
}
Mt(Yt, "createFocusScopesStack");
function Xt(e, t) {
	let n = [...e], r = n.indexOf(t);
	return r !== -1 && n.splice(r, 1), n;
}
Mt(Xt, "arrayRemove");
function Zt(e) {
	return e.filter((e) => e.tagName !== "A");
}
Mt(Zt, "removeLinks");
// node_modules/@radix-ui/react-portal/dist/index.mjs
var N = Object.defineProperty, Qt = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ ((e, t) => N(e, "name", {
	value: t,
	configurable: !0
}))(function(e, t) {
	let { container: n, ...r } = e, [i, a] = _.useState(!1);
	j(() => a(!0), []);
	let o = n || i && globalThis?.document?.body;
	return o ? he.createPortal(/* @__PURE__ */ (0, O.jsx)(ve.div, {
		...r,
		ref: t
	}), o) : null;
}, "Portal")), $t = Object.defineProperty, en = (e, t) => $t(e, "name", {
	value: t,
	configurable: !0
}), tn = 0, nn = null;
function rn(e) {
	return an(), e.children;
}
en(rn, "FocusGuards");
function an() {
	_.useEffect(() => {
		nn ||= {
			start: on(),
			end: on()
		};
		let { start: e, end: t } = nn;
		return document.body.firstElementChild !== e && document.body.insertAdjacentElement("afterbegin", e), document.body.lastElementChild !== t && document.body.insertAdjacentElement("beforeend", t), tn++, () => {
			tn === 1 && (nn?.start.remove(), nn?.end.remove(), nn = null), tn = Math.max(0, tn - 1);
		};
	}, []);
}
en(an, "useFocusGuards");
function on() {
	let e = document.createElement("span");
	return e.setAttribute("data-radix-focus-guard", ""), e.tabIndex = 0, e.style.outline = "none", e.style.opacity = "0", e.style.position = "fixed", e.style.pointerEvents = "none", e;
}
en(on, "createFocusGuard");
// node_modules/tslib/tslib.es6.mjs
var sn = function() {
	return sn = Object.assign || function(e) {
		for (var t, n = 1, r = arguments.length; n < r; n++) for (var i in t = arguments[n], t) Object.prototype.hasOwnProperty.call(t, i) && (e[i] = t[i]);
		return e;
	}, sn.apply(this, arguments);
};
function cn(e, t) {
	var n = {};
	for (var r in e) Object.prototype.hasOwnProperty.call(e, r) && t.indexOf(r) < 0 && (n[r] = e[r]);
	if (e != null && typeof Object.getOwnPropertySymbols == "function") for (var i = 0, r = Object.getOwnPropertySymbols(e); i < r.length; i++) t.indexOf(r[i]) < 0 && Object.prototype.propertyIsEnumerable.call(e, r[i]) && (n[r[i]] = e[r[i]]);
	return n;
}
function ln(e, t, n) {
	if (n || arguments.length === 2) for (var r = 0, i = t.length, a; r < i; r++) (a || !(r in t)) && (a ||= Array.prototype.slice.call(t, 0, r), a[r] = t[r]);
	return e.concat(a || Array.prototype.slice.call(t));
}
// node_modules/react-remove-scroll-bar/dist/es2015/constants.js
var un = "right-scroll-bar-position", dn = "width-before-scroll-bar", fn = "with-scroll-bars-hidden", pn = "--removed-body-scroll-bar-size";
// node_modules/use-callback-ref/dist/es2015/assignRef.js
function mn(e, t) {
	return typeof e == "function" ? e(t) : e && (e.current = t), e;
}
// node_modules/use-callback-ref/dist/es2015/useRef.js
function hn(e, t) {
	var n = (0, _.useState)(function() {
		return {
			value: e,
			callback: t,
			facade: {
				get current() {
					return n.value;
				},
				set current(e) {
					var t = n.value;
					t !== e && (n.value = e, n.callback(e, t));
				}
			}
		};
	})[0];
	return n.callback = t, n.facade;
}
// node_modules/use-callback-ref/dist/es2015/useMergeRef.js
var gn = typeof window < "u" ? _.useLayoutEffect : _.useEffect, _n = /* @__PURE__ */ new WeakMap();
function vn(e, t) {
	var n = hn(t || null, function(t) {
		return e.forEach(function(e) {
			return mn(e, t);
		});
	});
	return gn(function() {
		var t = _n.get(n);
		if (t) {
			var r = new Set(t), i = new Set(e), a = n.current;
			r.forEach(function(e) {
				i.has(e) || mn(e, null);
			}), i.forEach(function(e) {
				r.has(e) || mn(e, a);
			});
		}
		_n.set(n, e);
	}, [e]), n;
}
// node_modules/use-sidecar/dist/es2015/medium.js
function yn(e) {
	return e;
}
function bn(e, t) {
	t === void 0 && (t = yn);
	var n = [], r = !1;
	return {
		read: function() {
			if (r) throw Error("Sidecar: could not `read` from an `assigned` medium. `read` could be used only with `useMedium`.");
			return n.length ? n[n.length - 1] : e;
		},
		useMedium: function(e) {
			var i = t(e, r);
			return n.push(i), function() {
				n = n.filter(function(e) {
					return e !== i;
				});
			};
		},
		assignSyncMedium: function(e) {
			for (r = !0; n.length;) {
				var t = n;
				n = [], t.forEach(e);
			}
			n = {
				push: function(t) {
					return e(t);
				},
				filter: function() {
					return n;
				}
			};
		},
		assignMedium: function(e) {
			r = !0;
			var t = [];
			if (n.length) {
				var i = n;
				n = [], i.forEach(e), t = n;
			}
			var a = function() {
				var n = t;
				t = [], n.forEach(e);
			}, o = function() {
				return Promise.resolve().then(a);
			};
			o(), n = {
				push: function(e) {
					t.push(e), o();
				},
				filter: function(e) {
					return t = t.filter(e), n;
				}
			};
		}
	};
}
function xn(e) {
	e === void 0 && (e = {});
	var t = bn(null);
	return t.options = sn({
		async: !0,
		ssr: !1
	}, e), t;
}
// node_modules/use-sidecar/dist/es2015/exports.js
var Sn = function(e) {
	var t = e.sideCar, n = cn(e, ["sideCar"]);
	if (!t) throw Error("Sidecar: please provide `sideCar` property to import the right car");
	var r = t.read();
	if (!r) throw Error("Sidecar medium not found");
	return _.createElement(r, sn({}, n));
};
Sn.isSideCarExport = !0;
function Cn(e, t) {
	return e.useMedium(t), Sn;
}
// node_modules/react-remove-scroll/dist/es2015/medium.js
var wn = xn(), Tn = function() {}, En = _.forwardRef(function(e, t) {
	var n = _.useRef(null), r = _.useState({
		onScrollCapture: Tn,
		onWheelCapture: Tn,
		onTouchMoveCapture: Tn
	}), i = r[0], a = r[1], o = e.forwardProps, s = e.children, c = e.className, l = e.removeScrollBar, u = e.enabled, d = e.shards, f = e.sideCar, p = e.noRelative, m = e.noIsolation, h = e.inert, g = e.allowPinchZoom, v = e.as, y = v === void 0 ? "div" : v, b = e.gapMode, x = cn(e, [
		"forwardProps",
		"children",
		"className",
		"removeScrollBar",
		"enabled",
		"shards",
		"sideCar",
		"noRelative",
		"noIsolation",
		"inert",
		"allowPinchZoom",
		"as",
		"gapMode"
	]), S = f, C = vn([n, t]), w = sn(sn({}, x), i);
	return _.createElement(_.Fragment, null, u && _.createElement(S, {
		sideCar: wn,
		removeScrollBar: l,
		shards: d,
		noRelative: p,
		noIsolation: m,
		inert: h,
		setCallbacks: a,
		allowPinchZoom: !!g,
		lockRef: n,
		gapMode: b
	}), o ? _.cloneElement(_.Children.only(s), sn(sn({}, w), { ref: C })) : _.createElement(y, sn({}, w, {
		className: c,
		ref: C
	}), s));
});
En.defaultProps = {
	enabled: !0,
	removeScrollBar: !0,
	inert: !1
}, En.classNames = {
	fullWidth: dn,
	zeroRight: un
};
// node_modules/get-nonce/dist/es2015/index.js
var Dn = function() {
	if (typeof __webpack_nonce__ < "u") return __webpack_nonce__;
};
// node_modules/react-style-singleton/dist/es2015/singleton.js
function On() {
	if (!document) return null;
	var e = document.createElement("style");
	e.type = "text/css";
	var t = Dn();
	return t && e.setAttribute("nonce", t), e;
}
function kn(e, t) {
	e.styleSheet ? e.styleSheet.cssText = t : e.appendChild(document.createTextNode(t));
}
function An(e) {
	(document.head || document.getElementsByTagName("head")[0]).appendChild(e);
}
var jn = function() {
	var e = 0, t = null;
	return {
		add: function(n) {
			e == 0 && (t = On()) && (kn(t, n), An(t)), e++;
		},
		remove: function() {
			e--, !e && t && (t.parentNode && t.parentNode.removeChild(t), t = null);
		}
	};
}, Mn = function() {
	var e = jn();
	return function(t, n) {
		_.useEffect(function() {
			return e.add(t), function() {
				e.remove();
			};
		}, [t && n]);
	};
}, Nn = function() {
	var e = Mn();
	return function(t) {
		var n = t.styles, r = t.dynamic;
		return e(n, r), null;
	};
}, Pn = {
	left: 0,
	top: 0,
	right: 0,
	gap: 0
}, Fn = function(e) {
	return parseInt(e || "", 10) || 0;
}, In = function(e) {
	var t = window.getComputedStyle(document.body), n = t[e === "padding" ? "paddingLeft" : "marginLeft"], r = t[e === "padding" ? "paddingTop" : "marginTop"], i = t[e === "padding" ? "paddingRight" : "marginRight"];
	return [
		Fn(n),
		Fn(r),
		Fn(i)
	];
}, Ln = function(e) {
	if (e === void 0 && (e = "margin"), typeof window > "u") return Pn;
	var t = In(e), n = document.documentElement.clientWidth, r = window.innerWidth;
	return {
		left: t[0],
		top: t[1],
		right: t[2],
		gap: Math.max(0, r - n + t[2] - t[0])
	};
}, Rn = Nn(), zn = "data-scroll-locked", Bn = function(e, t, n, r) {
	var i = e.left, a = e.top, o = e.right, s = e.gap;
	return n === void 0 && (n = "margin"), `
  .${fn} {
   overflow: hidden ${r};
   padding-right: ${s}px ${r};
  }
  body[${zn}] {
    overflow: hidden ${r};
    overscroll-behavior: contain;
    ${[
		t && `position: relative ${r};`,
		n === "margin" && `
    padding-left: ${i}px;
    padding-top: ${a}px;
    padding-right: ${o}px;
    margin-left:0;
    margin-top:0;
    margin-right: ${s}px ${r};
    `,
		n === "padding" && `padding-right: ${s}px ${r};`
	].filter(Boolean).join("")}
  }
  
  .${un} {
    right: ${s}px ${r};
  }
  
  .${dn} {
    margin-right: ${s}px ${r};
  }
  
  .${un} .${un} {
    right: 0 ${r};
  }
  
  .${dn} .${dn} {
    margin-right: 0 ${r};
  }
  
  body[${zn}] {
    ${pn}: ${s}px;
  }
`;
}, Vn = function() {
	var e = parseInt(document.body.getAttribute("data-scroll-locked") || "0", 10);
	return isFinite(e) ? e : 0;
}, Hn = function() {
	_.useEffect(function() {
		return document.body.setAttribute(zn, (Vn() + 1).toString()), function() {
			var e = Vn() - 1;
			e <= 0 ? document.body.removeAttribute(zn) : document.body.setAttribute(zn, e.toString());
		};
	}, []);
}, Un = function(e) {
	var t = e.noRelative, n = e.noImportant, r = e.gapMode, i = r === void 0 ? "margin" : r;
	Hn();
	var a = _.useMemo(function() {
		return Ln(i);
	}, [i]);
	return _.createElement(Rn, { styles: Bn(a, !t, i, n ? "" : "!important") });
}, Wn = !1;
if (typeof window < "u") try {
	var Gn = Object.defineProperty({}, "passive", { get: function() {
		return Wn = !0, !0;
	} });
	window.addEventListener("test", Gn, Gn), window.removeEventListener("test", Gn, Gn);
} catch {
	Wn = !1;
}
var Kn = Wn ? { passive: !1 } : !1, qn = function(e) {
	return e.tagName === "TEXTAREA";
}, Jn = function(e, t) {
	if (!(e instanceof Element)) return !1;
	var n = window.getComputedStyle(e);
	return n[t] !== "hidden" && !(n.overflowY === n.overflowX && !qn(e) && n[t] === "visible");
}, Yn = function(e) {
	return Jn(e, "overflowY");
}, Xn = function(e) {
	return Jn(e, "overflowX");
}, Zn = function(e, t) {
	var n = t.ownerDocument, r = t;
	do {
		if (typeof ShadowRoot < "u" && r instanceof ShadowRoot && (r = r.host), er(e, r)) {
			var i = tr(e, r);
			if (i[1] > i[2]) return !0;
		}
		r = r.parentNode;
	} while (r && r !== n.body);
	return !1;
}, Qn = function(e) {
	return [
		e.scrollTop,
		e.scrollHeight,
		e.clientHeight
	];
}, $n = function(e) {
	return [
		e.scrollLeft,
		e.scrollWidth,
		e.clientWidth
	];
}, er = function(e, t) {
	return e === "v" ? Yn(t) : Xn(t);
}, tr = function(e, t) {
	return e === "v" ? Qn(t) : $n(t);
}, nr = function(e, t) {
	return e === "h" && t === "rtl" ? -1 : 1;
}, rr = function(e, t, n, r, i) {
	var a = nr(e, window.getComputedStyle(t).direction), o = a * r, s = n.target, c = t.contains(s), l = !1, u = o > 0, d = 0, f = 0;
	do {
		if (!s) break;
		var p = tr(e, s), m = p[0], h = p[1] - p[2] - a * m;
		(m || h) && er(e, s) && (d += h, f += m);
		var g = s.parentNode;
		s = g && g.nodeType === Node.DOCUMENT_FRAGMENT_NODE ? g.host : g;
	} while (!c && s !== document.body || c && (t.contains(s) || t === s));
	return (u && (i && Math.abs(d) < 1 || !i && o > d) || !u && (i && Math.abs(f) < 1 || !i && -o > f)) && (l = !0), l;
}, ir = function(e) {
	return "changedTouches" in e ? [e.changedTouches[0].clientX, e.changedTouches[0].clientY] : [0, 0];
}, ar = function(e) {
	return [e.deltaX, e.deltaY];
}, or = function(e) {
	return e && "current" in e ? e.current : e;
}, sr = function(e, t) {
	return e[0] === t[0] && e[1] === t[1];
}, cr = function(e) {
	return `
  .block-interactivity-${e} {pointer-events: none;}
  .allow-interactivity-${e} {pointer-events: all;}
`;
}, lr = 0, ur = [];
function dr(e) {
	var t = _.useRef([]), n = _.useRef([0, 0]), r = _.useRef(), i = _.useState(lr++)[0], a = _.useState(Nn)[0], o = _.useRef(e);
	_.useEffect(function() {
		o.current = e;
	}, [e]), _.useEffect(function() {
		if (e.inert) {
			document.body.classList.add(`block-interactivity-${i}`);
			var t = ln([e.lockRef.current], (e.shards || []).map(or), !0).filter(Boolean);
			return t.forEach(function(e) {
				return e.classList.add(`allow-interactivity-${i}`);
			}), function() {
				document.body.classList.remove(`block-interactivity-${i}`), t.forEach(function(e) {
					return e.classList.remove(`allow-interactivity-${i}`);
				});
			};
		}
	}, [
		e.inert,
		e.lockRef.current,
		e.shards
	]);
	var s = _.useCallback(function(e, t) {
		if ("touches" in e && e.touches.length === 2 || e.type === "wheel" && e.ctrlKey) return !o.current.allowPinchZoom;
		var i = ir(e), a = n.current, s = "deltaX" in e ? e.deltaX : a[0] - i[0], c = "deltaY" in e ? e.deltaY : a[1] - i[1], l, u = e.target, d = Math.abs(s) > Math.abs(c) ? "h" : "v";
		if ("touches" in e && d === "h" && u.type === "range") return !1;
		var f = window.getSelection(), p = f && f.anchorNode;
		if (p && (p === u || p.contains(u))) return !1;
		var m = Zn(d, u);
		if (!m) return !0;
		if (m ? l = d : (l = d === "v" ? "h" : "v", m = Zn(d, u)), !m) return !1;
		if (!r.current && "changedTouches" in e && (s || c) && (r.current = l), !l) return !0;
		var h = r.current || l;
		return rr(h, t, e, h === "h" ? s : c, !0);
	}, []), c = _.useCallback(function(e) {
		var n = e;
		if (ur.length && ur[ur.length - 1] === a) {
			var r = "deltaY" in n ? ar(n) : ir(n), i = t.current.filter(function(e) {
				return e.name === n.type && (e.target === n.target || n.target === e.shadowParent) && sr(e.delta, r);
			})[0];
			if (i && i.should) n.cancelable && n.preventDefault();
			else if (!i) {
				var c = (o.current.shards || []).map(or).filter(Boolean).filter(function(e) {
					return e.contains(n.target);
				});
				(c.length > 0 ? s(n, c[0]) : !o.current.noIsolation) && n.cancelable && n.preventDefault();
			}
		}
	}, []), l = _.useCallback(function(e, n, r, i) {
		var a = {
			name: e,
			delta: n,
			target: r,
			should: i,
			shadowParent: fr(r)
		};
		t.current.push(a), setTimeout(function() {
			t.current = t.current.filter(function(e) {
				return e !== a;
			});
		}, 1);
	}, []), u = _.useCallback(function(e) {
		n.current = ir(e), r.current = void 0;
	}, []), d = _.useCallback(function(t) {
		l(t.type, ar(t), t.target, s(t, e.lockRef.current));
	}, []), f = _.useCallback(function(t) {
		l(t.type, ir(t), t.target, s(t, e.lockRef.current));
	}, []);
	_.useEffect(function() {
		return ur.push(a), e.setCallbacks({
			onScrollCapture: d,
			onWheelCapture: d,
			onTouchMoveCapture: f
		}), document.addEventListener("wheel", c, Kn), document.addEventListener("touchmove", c, Kn), document.addEventListener("touchstart", u, Kn), function() {
			ur = ur.filter(function(e) {
				return e !== a;
			}), document.removeEventListener("wheel", c, Kn), document.removeEventListener("touchmove", c, Kn), document.removeEventListener("touchstart", u, Kn);
		};
	}, []);
	var p = e.removeScrollBar, m = e.inert;
	return _.createElement(_.Fragment, null, m ? _.createElement(a, { styles: cr(i) }) : null, p ? _.createElement(Un, {
		noRelative: e.noRelative,
		gapMode: e.gapMode
	}) : null);
}
function fr(e) {
	for (var t = null; e !== null;) e instanceof ShadowRoot && (t = e.host, e = e.host), e = e.parentNode;
	return t;
}
// node_modules/react-remove-scroll/dist/es2015/sidecar.js
var pr = Cn(wn, dr), mr = _.forwardRef(function(e, t) {
	return _.createElement(En, sn({}, e, {
		ref: t,
		sideCar: pr
	}));
});
mr.classNames = En.classNames;
// node_modules/aria-hidden/dist/es2015/index.js
var hr = function(e) {
	return typeof document > "u" ? null : (Array.isArray(e) ? e[0] : e).ownerDocument.body;
}, gr = /* @__PURE__ */ new WeakMap(), _r = /* @__PURE__ */ new WeakMap(), vr = {}, yr = 0, br = function(e) {
	return e && (e.host || br(e.parentNode));
}, xr = function(e, t) {
	return t.map(function(t) {
		if (e.contains(t)) return t;
		var n = br(t);
		return n && e.contains(n) ? n : (console.error("aria-hidden", t, "in not contained inside", e, ". Doing nothing"), null);
	}).filter(function(e) {
		return !!e;
	});
}, Sr = function(e, t, n, r) {
	var i = xr(t, Array.isArray(e) ? e : [e]);
	vr[n] || (vr[n] = /* @__PURE__ */ new WeakMap());
	var a = vr[n], o = [], s = /* @__PURE__ */ new Set(), c = new Set(i), l = function(e) {
		e && !s.has(e) && (s.add(e), l(e.parentNode));
	};
	i.forEach(l);
	var u = function(e) {
		e && !c.has(e) && Array.prototype.forEach.call(e.children, function(e) {
			if (s.has(e)) u(e);
			else try {
				var t = e.getAttribute(r), i = t !== null && t !== "false", c = (gr.get(e) || 0) + 1, l = (a.get(e) || 0) + 1;
				gr.set(e, c), a.set(e, l), o.push(e), c === 1 && i && _r.set(e, !0), l === 1 && e.setAttribute(n, "true"), i || e.setAttribute(r, "true");
			} catch (t) {
				console.error("aria-hidden: cannot operate on ", e, t);
			}
		});
	};
	return u(t), s.clear(), yr++, function() {
		o.forEach(function(e) {
			var t = gr.get(e) - 1, i = a.get(e) - 1;
			gr.set(e, t), a.set(e, i), t || (_r.has(e) || e.removeAttribute(r), _r.delete(e)), i || e.removeAttribute(n);
		}), yr--, yr || (gr = /* @__PURE__ */ new WeakMap(), gr = /* @__PURE__ */ new WeakMap(), _r = /* @__PURE__ */ new WeakMap(), vr = {});
	};
}, Cr = function(e, t, n) {
	n === void 0 && (n = "data-aria-hidden");
	var r = Array.from(Array.isArray(e) ? e : [e]), i = t || hr(e);
	return i ? (r.push.apply(r, Array.from(i.querySelectorAll("[aria-live], script"))), Sr(r, i, n, "aria-hidden")) : function() {
		return null;
	};
}, wr = Object.defineProperty, Tr = (e, t) => wr(e, "name", {
	value: t,
	configurable: !0
});
function Er(e) {
	let [t, n] = _.useState(void 0);
	return j(() => {
		if (e) {
			n({
				width: e.offsetWidth,
				height: e.offsetHeight
			});
			let t = 0, r = new ResizeObserver((r) => {
				if (!Array.isArray(r) || !r.length) return;
				let i = r[0];
				window.cancelAnimationFrame(t), t = window.requestAnimationFrame(() => {
					let t, r;
					if ("borderBoxSize" in i) {
						let e = i.borderBoxSize, n = Array.isArray(e) ? e[0] : e;
						t = n.inlineSize, r = n.blockSize;
					} else t = e.offsetWidth, r = e.offsetHeight;
					n({
						width: t,
						height: r
					});
				});
			});
			return r.observe(e, { box: "border-box" }), () => {
				window.cancelAnimationFrame(t), r.unobserve(e);
			};
		}
		n(void 0);
	}, [e]), t;
}
Tr(Er, "useSize");
// node_modules/@floating-ui/utils/dist/floating-ui.utils.mjs
var Dr = [
	"top",
	"right",
	"bottom",
	"left"
], Or = Math.min, kr = Math.max, Ar = Math.round, jr = Math.floor, Mr = (e) => ({
	x: e,
	y: e
}), Nr = {
	left: "right",
	right: "left",
	bottom: "top",
	top: "bottom"
};
function Pr(e, t, n) {
	return kr(e, Or(t, n));
}
function Fr(e, t) {
	return typeof e == "function" ? e(t) : e;
}
function Ir(e) {
	return e.split("-")[0];
}
function Lr(e) {
	return e.split("-")[1];
}
function Rr(e) {
	return e === "x" ? "y" : "x";
}
function zr(e) {
	return e === "y" ? "height" : "width";
}
function Br(e) {
	let t = e[0];
	return t === "t" || t === "b" ? "y" : "x";
}
function Vr(e) {
	return Rr(Br(e));
}
function Hr(e, t, n) {
	n === void 0 && (n = !1);
	let r = Lr(e), i = Vr(e), a = zr(i), o = i === "x" ? r === (n ? "end" : "start") ? "right" : "left" : r === "start" ? "bottom" : "top";
	return t.reference[a] > t.floating[a] && (o = Zr(o)), [o, Zr(o)];
}
function Ur(e) {
	let t = Zr(e);
	return [
		Wr(e),
		t,
		Wr(t)
	];
}
function Wr(e) {
	return e.includes("start") ? e.replace("start", "end") : e.replace("end", "start");
}
var Gr = ["left", "right"], Kr = ["right", "left"], qr = ["top", "bottom"], Jr = ["bottom", "top"];
function Yr(e, t, n) {
	switch (e) {
		case "top":
		case "bottom": return n ? t ? Kr : Gr : t ? Gr : Kr;
		case "left":
		case "right": return t ? qr : Jr;
		default: return [];
	}
}
function Xr(e, t, n, r) {
	let i = Lr(e), a = Yr(Ir(e), n === "start", r);
	return i && (a = a.map((e) => e + "-" + i), t && (a = a.concat(a.map(Wr)))), a;
}
function Zr(e) {
	let t = Ir(e);
	return Nr[t] + e.slice(t.length);
}
function Qr(e) {
	return {
		top: e.top ?? 0,
		right: e.right ?? 0,
		bottom: e.bottom ?? 0,
		left: e.left ?? 0
	};
}
function $r(e) {
	return typeof e == "number" ? {
		top: e,
		right: e,
		bottom: e,
		left: e
	} : Qr(e);
}
function ei(e) {
	let { x: t, y: n, width: r, height: i } = e;
	return {
		width: r,
		height: i,
		top: n,
		left: t,
		right: t + r,
		bottom: n + i,
		x: t,
		y: n
	};
}
// node_modules/@floating-ui/core/dist/floating-ui.core.mjs
function ti(e, t, n) {
	let { reference: r, floating: i } = e, a = Br(t), o = Vr(t), s = zr(o), c = Ir(t), l = a === "y", u = r.x + r.width / 2 - i.width / 2, d = r.y + r.height / 2 - i.height / 2, f = r[s] / 2 - i[s] / 2, p;
	switch (c) {
		case "top":
			p = {
				x: u,
				y: r.y - i.height
			};
			break;
		case "bottom":
			p = {
				x: u,
				y: r.y + r.height
			};
			break;
		case "right":
			p = {
				x: r.x + r.width,
				y: d
			};
			break;
		case "left":
			p = {
				x: r.x - i.width,
				y: d
			};
			break;
		default: p = {
			x: r.x,
			y: r.y
		};
	}
	let m = Lr(t);
	return m && (p[o] += f * (m === "end" ? 1 : -1) * (n && l ? -1 : 1)), p;
}
async function ni(e, t) {
	t === void 0 && (t = {});
	let { x: n, y: r, platform: i, rects: a, elements: o, strategy: s } = e, { boundary: c = "clippingAncestors", rootBoundary: l = "viewport", elementContext: u = "floating", altBoundary: d = !1, padding: f = 0 } = Fr(t, e), p = $r(f), m = o[d ? u === "floating" ? "reference" : "floating" : u], h = ei(await i.getClippingRect({
		element: await (i.isElement == null ? void 0 : i.isElement(m)) ?? !0 ? m : m.contextElement || await (i.getDocumentElement == null ? void 0 : i.getDocumentElement(o.floating)),
		boundary: c,
		rootBoundary: l,
		strategy: s
	})), g = u === "floating" ? {
		x: n,
		y: r,
		width: a.floating.width,
		height: a.floating.height
	} : a.reference, _ = await (i.getOffsetParent == null ? void 0 : i.getOffsetParent(o.floating)), v = await (i.isElement == null ? void 0 : i.isElement(_)) && await (i.getScale == null ? void 0 : i.getScale(_)) || {
		x: 1,
		y: 1
	}, y = ei(i.convertOffsetParentRelativeRectToViewportRelativeRect ? await i.convertOffsetParentRelativeRectToViewportRelativeRect({
		elements: o,
		rect: g,
		offsetParent: _,
		strategy: s
	}) : g);
	return {
		top: (h.top - y.top + p.top) / v.y,
		bottom: (y.bottom - h.bottom + p.bottom) / v.y,
		left: (h.left - y.left + p.left) / v.x,
		right: (y.right - h.right + p.right) / v.x
	};
}
var ri = 50, ii = async (e, t, n) => {
	let { placement: r = "bottom", strategy: i = "absolute", middleware: a = [], platform: o } = n, s = o.detectOverflow ? o : {
		...o,
		detectOverflow: ni
	}, c = await (o.isRTL == null ? void 0 : o.isRTL(t)), l = await o.getElementRects({
		reference: e,
		floating: t,
		strategy: i
	}), { x: u, y: d } = ti(l, r, c), f = r, p = 0, m = {};
	for (let n = 0; n < a.length; n++) {
		let h = a[n];
		if (!h) continue;
		let { name: g, fn: _ } = h, { x: v, y, data: b, reset: x } = await _({
			x: u,
			y: d,
			initialPlacement: r,
			placement: f,
			strategy: i,
			middlewareData: m,
			rects: l,
			platform: s,
			elements: {
				reference: e,
				floating: t
			}
		});
		u = v ?? u, d = y ?? d, m[g] = {
			...m[g],
			...b
		}, x && p < ri && (p++, typeof x == "object" && (x.placement && (f = x.placement), x.rects && (l = x.rects === !0 ? await o.getElementRects({
			reference: e,
			floating: t,
			strategy: i
		}) : x.rects), {x: u, y: d} = ti(l, f, c)), n = -1);
	}
	return {
		x: u,
		y: d,
		placement: f,
		strategy: i,
		middlewareData: m
	};
}, ai = (e) => ({
	name: "arrow",
	options: e,
	async fn(t) {
		let { x: n, y: r, placement: i, rects: a, platform: o, elements: s, middlewareData: c } = t, { element: l, padding: u = 0 } = Fr(e, t) || {};
		if (l == null) return {};
		let d = $r(u), f = {
			x: n,
			y: r
		}, p = Vr(i), m = zr(p), h = await o.getDimensions(l), g = p === "y", _ = g ? "top" : "left", v = g ? "bottom" : "right", y = g ? "clientHeight" : "clientWidth", b = a.reference[m] + a.reference[p] - f[p] - a.floating[m], x = f[p] - a.reference[p], S = await (o.getOffsetParent == null ? void 0 : o.getOffsetParent(l)), C = S ? S[y] : 0;
		C && await (o.isElement == null ? void 0 : o.isElement(S)) || (C = s.floating[y] || a.floating[m]);
		let w = b / 2 - x / 2, T = C / 2 - h[m] / 2 - 1, ee = Or(d[_], T), E = Or(d[v], T), te = C - h[m] - E, ne = C / 2 - h[m] / 2 + w, re = Pr(ee, ne, te), D = !c.arrow && Lr(i) != null && ne !== re && a.reference[m] / 2 - (ne < ee ? ee : E) - h[m] / 2 < 0, ie = D ? ne < ee ? ne - ee : ne - te : 0;
		return {
			[p]: f[p] + ie,
			data: {
				[p]: re,
				centerOffset: ne - re - ie,
				...D && { alignmentOffset: ie }
			},
			reset: D
		};
	}
}), oi = function(e) {
	return e === void 0 && (e = {}), {
		name: "flip",
		options: e,
		async fn(t) {
			var n;
			let { placement: r, middlewareData: i, rects: a, initialPlacement: o, platform: s, elements: c } = t, { mainAxis: l = !0, crossAxis: u = !0, fallbackPlacements: d, fallbackStrategy: f = "bestFit", fallbackAxisSideDirection: p = "none", flipAlignment: m = !0, ...h } = Fr(e, t);
			if ((n = i.arrow) != null && n.alignmentOffset) return {};
			let g = Ir(r), _ = Br(o), v = Ir(o) === o, y = await (s.isRTL == null ? void 0 : s.isRTL(c.floating)), b = d || (v || !m ? [Zr(o)] : Ur(o)), x = p !== "none";
			!d && x && b.push(...Xr(o, m, p, y));
			let S = [o, ...b], C = await s.detectOverflow(t, h), w = [], T = i.flip?.overflows || [];
			if (l && w.push(C[g]), u) {
				let e = Hr(r, a, y);
				w.push(C[e[0]], C[e[1]]);
			}
			if (T = [...T, {
				placement: r,
				overflows: w
			}], !w.every((e) => e <= 0)) {
				let e = (i.flip?.index || 0) + 1, t = S[e];
				if (t && (u !== "alignment" || _ === Br(t) || T.every((e) => Br(e.placement) !== _ || e.overflows[0] > 0))) return {
					data: {
						index: e,
						overflows: T
					},
					reset: { placement: t }
				};
				let n = T.filter((e) => e.overflows[0] <= 0).sort((e, t) => e.overflows[1] - t.overflows[1])[0]?.placement;
				if (!n) switch (f) {
					case "bestFit": {
						let e = T.filter((e) => {
							if (x) {
								let t = Br(e.placement);
								return t === _ || t === "y";
							}
							return !0;
						}).map((e) => [e.placement, e.overflows.filter((e) => e > 0).reduce((e, t) => e + t, 0)]).sort((e, t) => e[1] - t[1])[0]?.[0];
						e && (n = e);
						break;
					}
					case "initialPlacement": n = o;
				}
				if (r !== n) return { reset: { placement: n } };
			}
			return {};
		}
	};
};
function si(e, t) {
	return {
		top: e.top - t.height,
		right: e.right - t.width,
		bottom: e.bottom - t.height,
		left: e.left - t.width
	};
}
function ci(e) {
	return Dr.some((t) => e[t] >= 0);
}
var li = function(e) {
	return e === void 0 && (e = {}), {
		name: "hide",
		options: e,
		async fn(t) {
			let { rects: n, platform: r } = t, { strategy: i = "referenceHidden", ...a } = Fr(e, t);
			switch (i) {
				case "referenceHidden": {
					let e = si(await r.detectOverflow(t, {
						...a,
						elementContext: "reference"
					}), n.reference);
					return { data: {
						referenceHiddenOffsets: e,
						referenceHidden: ci(e)
					} };
				}
				case "escaped": {
					let e = si(await r.detectOverflow(t, {
						...a,
						altBoundary: !0
					}), n.floating);
					return { data: {
						escapedOffsets: e,
						escaped: ci(e)
					} };
				}
				default: return {};
			}
		}
	};
}, ui = /*#__PURE__*/ new Set(["left", "top"]);
async function di(e, t) {
	let { placement: n, platform: r, elements: i } = e, a = await (r.isRTL == null ? void 0 : r.isRTL(i.floating)), o = Ir(n), s = Lr(n), c = Br(n) === "y", l = ui.has(o) ? -1 : 1, u = a && c ? -1 : 1, d = Fr(t, e), { mainAxis: f, crossAxis: p, alignmentAxis: m } = typeof d == "number" ? {
		mainAxis: d,
		crossAxis: 0,
		alignmentAxis: null
	} : {
		mainAxis: d.mainAxis || 0,
		crossAxis: d.crossAxis || 0,
		alignmentAxis: d.alignmentAxis
	};
	return s && typeof m == "number" && (p = s === "end" ? m * -1 : m), c ? {
		x: p * u,
		y: f * l
	} : {
		x: f * l,
		y: p * u
	};
}
var fi = function(e) {
	return e === void 0 && (e = 0), {
		name: "offset",
		options: e,
		async fn(t) {
			var n;
			let { x: r, y: i, placement: a, middlewareData: o } = t, s = await di(t, e);
			return a === o.offset?.placement && (n = o.arrow) != null && n.alignmentOffset ? {} : {
				x: r + s.x,
				y: i + s.y,
				data: {
					...s,
					placement: a
				}
			};
		}
	};
}, pi = function(e) {
	return e === void 0 && (e = {}), {
		name: "shift",
		options: e,
		async fn(t) {
			let { x: n, y: r, placement: i, platform: a } = t, { mainAxis: o = !0, crossAxis: s = !1, limiter: c = { fn: (e) => {
				let { x: t, y: n } = e;
				return {
					x: t,
					y: n
				};
			} }, ...l } = Fr(e, t), u = {
				x: n,
				y: r
			}, d = await a.detectOverflow(t, l), f = Br(i), p = Rr(f), m = u[p], h = u[f], g = (e, t) => Pr(t + d[e === "y" ? "top" : "left"], t, t - d[e === "y" ? "bottom" : "right"]);
			o && (m = g(p, m)), s && (h = g(f, h));
			let _ = c.fn({
				...t,
				[p]: m,
				[f]: h
			});
			return {
				..._,
				data: {
					x: _.x - n,
					y: _.y - r,
					enabled: {
						[p]: o,
						[f]: s
					}
				}
			};
		}
	};
}, mi = function(e) {
	return e === void 0 && (e = {}), {
		options: e,
		fn(t) {
			let { x: n, y: r, placement: i, rects: a, middlewareData: o } = t, { offset: s = 0, mainAxis: c = !0, crossAxis: l = !0 } = Fr(e, t), u = {
				x: n,
				y: r
			}, d = Br(i), f = Rr(d), p = u[f], m = u[d], h = Fr(s, t), g = typeof h == "number" ? {
				mainAxis: h,
				crossAxis: 0
			} : {
				mainAxis: h.mainAxis ?? 0,
				crossAxis: h.crossAxis ?? 0
			};
			if (c) {
				let e = f === "y" ? "height" : "width", t = a.reference[f] - a.floating[e] + g.mainAxis, n = a.reference[f] + a.reference[e] - g.mainAxis;
				p < t ? p = t : p > n && (p = n);
			}
			if (l) {
				let e = f === "y" ? "width" : "height", t = ui.has(Ir(i)), n = a.reference[d] - a.floating[e] + (t && o.offset?.[d] || 0) + (t ? 0 : g.crossAxis), r = a.reference[d] + a.reference[e] + (t ? 0 : o.offset?.[d] || 0) - (t ? g.crossAxis : 0);
				m < n ? m = n : m > r && (m = r);
			}
			return {
				[f]: p,
				[d]: m
			};
		}
	};
}, hi = function(e) {
	return e === void 0 && (e = {}), {
		name: "size",
		options: e,
		async fn(t) {
			let { placement: n, rects: r, platform: i, elements: a } = t, { apply: o = () => {}, ...s } = Fr(e, t), c = await i.detectOverflow(t, s), l = Ir(n), u = Lr(n), d = Br(n) === "y", { width: f, height: p } = r.floating, m, h;
			l === "top" || l === "bottom" ? (m = l, h = u === (await (i.isRTL == null ? void 0 : i.isRTL(a.floating)) ? "start" : "end") ? "left" : "right") : (h = l, m = u === "end" ? "top" : "bottom");
			let g = p - c.top - c.bottom, _ = f - c.left - c.right, v = Or(p - c[m], g), y = Or(f - c[h], _), b = t.middlewareData.shift, x = !b, S = v, C = y;
			b != null && b.enabled.x && (C = _), b != null && b.enabled.y && (S = g), x && !u && (d ? C = f - 2 * kr(c.left, c.right) : S = p - 2 * kr(c.top, c.bottom)), await o({
				...t,
				availableWidth: C,
				availableHeight: S
			});
			let w = await i.getDimensions(a.floating);
			return f !== w.width || p !== w.height ? { reset: { rects: !0 } } : {};
		}
	};
};
// node_modules/@floating-ui/utils/dist/floating-ui.utils.dom.mjs
function gi() {
	return typeof window < "u";
}
function _i(e) {
	return bi(e) ? (e.nodeName || "").toLowerCase() : "#document";
}
function vi(e) {
	var t;
	return (e == null || (t = e.ownerDocument) == null ? void 0 : t.defaultView) || window;
}
function yi(e) {
	return ((bi(e) ? e.ownerDocument : e.document) || window.document)?.documentElement;
}
function bi(e) {
	return gi() ? e instanceof Node || e instanceof vi(e).Node : !1;
}
function xi(e) {
	return gi() ? e instanceof Element || e instanceof vi(e).Element : !1;
}
function Si(e) {
	return gi() ? e instanceof HTMLElement || e instanceof vi(e).HTMLElement : !1;
}
function Ci(e) {
	return !gi() || typeof ShadowRoot > "u" ? !1 : e instanceof ShadowRoot || e instanceof vi(e).ShadowRoot;
}
function wi(e) {
	let { overflow: t, overflowX: n, overflowY: r, display: i } = Fi(e);
	return /auto|scroll|overlay|hidden|clip/.test(t + r + n) && i !== "inline" && i !== "contents";
}
function Ti(e) {
	return /^(table|td|th)$/.test(_i(e));
}
function Ei(e) {
	try {
		if (e.matches(":popover-open")) return !0;
	} catch {}
	try {
		return e.matches(":modal");
	} catch {
		return !1;
	}
}
var Di = /transform|translate|scale|rotate|perspective|filter/, Oi = /paint|layout|strict|content/, ki = (e) => !!e && e !== "none", Ai;
function ji(e) {
	let t = xi(e) ? Fi(e) : e;
	return ki(t.transform) || ki(t.translate) || ki(t.scale) || ki(t.rotate) || ki(t.perspective) || !Ni() && (ki(t.backdropFilter) || ki(t.filter)) || Di.test(t.willChange || "") || Oi.test(t.contain || "");
}
function Mi(e) {
	let t = Li(e);
	for (; Si(t) && !Pi(t);) {
		if (ji(t)) return t;
		if (Ei(t)) return null;
		t = Li(t);
	}
	return null;
}
function Ni() {
	return Ai ??= typeof CSS < "u" && CSS.supports && CSS.supports("-webkit-backdrop-filter", "none"), Ai;
}
function Pi(e) {
	return /^(html|body|#document)$/.test(_i(e));
}
function Fi(e) {
	return vi(e).getComputedStyle(e);
}
function Ii(e) {
	return xi(e) ? {
		scrollLeft: e.scrollLeft,
		scrollTop: e.scrollTop
	} : {
		scrollLeft: e.scrollX,
		scrollTop: e.scrollY
	};
}
function Li(e) {
	if (_i(e) === "html") return e;
	let t = e.assignedSlot || e.parentNode || Ci(e) && e.host || yi(e);
	return Ci(t) ? t.host : t;
}
function Ri(e) {
	let t = Li(e);
	return Pi(t) ? (e.ownerDocument || e).body : Si(t) && wi(t) ? t : Ri(t);
}
function zi(e, t, n) {
	t === void 0 && (t = []), n === void 0 && (n = !0);
	let r = Ri(e), i = r === e.ownerDocument?.body, a = vi(r);
	if (i) {
		let e = Bi(a);
		return t.concat(a, a.visualViewport || [], wi(r) ? r : [], e && n ? zi(e) : []);
	}
	return t.concat(r, zi(r, [], n));
}
function Bi(e) {
	return e.parent && Object.getPrototypeOf(e.parent) ? e.frameElement : null;
}
// node_modules/@floating-ui/dom/dist/floating-ui.dom.mjs
function Vi(e) {
	let t = Fi(e), n = parseFloat(t.width) || 0, r = parseFloat(t.height) || 0, i = Si(e), a = i ? e.offsetWidth : n, o = i ? e.offsetHeight : r, s = Ar(n) !== a || Ar(r) !== o;
	return s && (n = a, r = o), {
		width: n,
		height: r,
		$: s
	};
}
function Hi(e) {
	return xi(e) ? e : e.contextElement;
}
function Ui(e) {
	let t = Hi(e);
	if (!Si(t)) return Mr(1);
	let n = t.getBoundingClientRect(), { width: r, height: i, $: a } = Vi(t), o = (a ? Ar(n.width) : n.width) / r, s = (a ? Ar(n.height) : n.height) / i;
	return o && Number.isFinite(o) || (o = 1), s && Number.isFinite(s) || (s = 1), {
		x: o,
		y: s
	};
}
var Wi = /*#__PURE__*/ Mr(0);
function Gi(e) {
	let t = vi(e);
	return !Ni() || !t.visualViewport ? Wi : {
		x: t.visualViewport.offsetLeft,
		y: t.visualViewport.offsetTop
	};
}
function Ki(e, t, n) {
	return t === void 0 && (t = !1), !!n && t && n === vi(e);
}
function qi(e, t, n, r) {
	t === void 0 && (t = !1), n === void 0 && (n = !1);
	let i = e.getBoundingClientRect(), a = Hi(e), o = Mr(1);
	t && (r ? xi(r) && (o = Ui(r)) : o = Ui(e));
	let s = Ki(a, n, r) ? Gi(a) : Mr(0), c = (i.left + s.x) / o.x, l = (i.top + s.y) / o.y, u = i.width / o.x, d = i.height / o.y;
	if (a && r) {
		let e = vi(a), t = xi(r) ? vi(r) : r, n = e, i = Bi(n);
		for (; i && t !== n;) {
			let e = Ui(i), t = i.getBoundingClientRect(), r = Fi(i), a = t.left + (i.clientLeft + parseFloat(r.paddingLeft)) * e.x, o = t.top + (i.clientTop + parseFloat(r.paddingTop)) * e.y;
			c *= e.x, l *= e.y, u *= e.x, d *= e.y, c += a, l += o, n = vi(i), i = Bi(n);
		}
	}
	return ei({
		width: u,
		height: d,
		x: c,
		y: l
	});
}
function Ji(e, t) {
	let n = Ii(e).scrollLeft;
	return t ? t.left + n : qi(yi(e)).left + n;
}
function Yi(e, t) {
	let n = e.getBoundingClientRect();
	return {
		x: n.left + t.scrollLeft - Ji(e, n),
		y: n.top + t.scrollTop
	};
}
function Xi(e) {
	let { elements: t, rect: n, offsetParent: r, strategy: i } = e, a = i === "fixed", o = yi(r), s = t ? Ei(t.floating) : !1;
	if (r === o || s && a) return n;
	let c = {
		scrollLeft: 0,
		scrollTop: 0
	}, l = Mr(1), u = Mr(0), d = Si(r);
	if ((d || !a) && ((_i(r) !== "body" || wi(o)) && (c = Ii(r)), d)) {
		let e = qi(r);
		l = Ui(r), u.x = e.x + r.clientLeft, u.y = e.y + r.clientTop;
	}
	let f = o && !d && !a ? Yi(o, c) : Mr(0);
	return {
		width: n.width * l.x,
		height: n.height * l.y,
		x: n.x * l.x - c.scrollLeft * l.x + u.x + f.x,
		y: n.y * l.y - c.scrollTop * l.y + u.y + f.y
	};
}
function Zi(e) {
	return e.getClientRects ? Array.from(e.getClientRects()) : [];
}
function Qi(e) {
	let t = Ii(e), n = e.ownerDocument.body, r = kr(e.scrollWidth, e.clientWidth, n.scrollWidth, n.clientWidth), i = kr(e.scrollHeight, e.clientHeight, n.scrollHeight, n.clientHeight), a = -t.scrollLeft + Ji(e), o = -t.scrollTop;
	return Fi(n).direction === "rtl" && (a += kr(e.clientWidth, n.clientWidth) - r), {
		width: r,
		height: i,
		x: a,
		y: o
	};
}
var $i = 25;
function ea(e, t, n) {
	n === void 0 && (n = "viewport");
	let r = n === "layoutViewport", i = vi(e), a = yi(e), o = i.visualViewport, s = a.clientWidth, c = a.clientHeight, l = 0, u = 0;
	if (o) {
		let e = !Ni() || t === "fixed";
		r ? e || (l = -o.offsetLeft, u = -o.offsetTop) : (s = o.width, c = o.height, e && (l = o.offsetLeft, u = o.offsetTop));
	}
	if (Ji(a) <= 0) {
		let e = a.ownerDocument, t = e.body, n = getComputedStyle(t), r = e.compatMode === "CSS1Compat" && parseFloat(n.marginLeft) + parseFloat(n.marginRight) || 0, i = Math.abs(a.clientWidth - t.clientWidth - r), o = getComputedStyle(a).scrollbarGutter === "stable both-edges" ? i / 2 : i;
		o <= $i && (s -= o);
	}
	return {
		width: s,
		height: c,
		x: l,
		y: u
	};
}
function ta(e, t) {
	let n = qi(e, !0, t === "fixed"), r = n.top + e.clientTop, i = n.left + e.clientLeft, a = Ui(e);
	return {
		width: e.clientWidth * a.x,
		height: e.clientHeight * a.y,
		x: i * a.x,
		y: r * a.y
	};
}
function na(e, t, n) {
	let r;
	if (t === "viewport" || t === "layoutViewport") r = ea(e, n, t);
	else if (t === "document") r = Qi(yi(e));
	else if (xi(t)) r = ta(t, n);
	else {
		let n = Gi(e);
		r = {
			x: t.x - n.x,
			y: t.y - n.y,
			width: t.width,
			height: t.height
		};
	}
	return ei(r);
}
function ra(e, t) {
	let n = t.get(e);
	if (n) return n;
	let r = zi(e, [], !1).filter((e) => xi(e) && _i(e) !== "body"), i = null, a = Fi(e).position === "fixed", o = a ? Li(e) : e;
	for (; xi(o) && !Pi(o);) {
		let e = Fi(o), t = ji(o), n = i ? i.position : a ? "fixed" : "";
		!t && (n === "fixed" || n === "absolute" && e.position === "static") ? r = r.filter((e) => e !== o) : i = e, o = Li(o);
	}
	return t.set(e, r), r;
}
function ia(e) {
	let { element: t, boundary: n, rootBoundary: r, strategy: i } = e, a = [...n === "clippingAncestors" ? Ei(t) ? [] : ra(t, this._c) : [].concat(n), r], o = na(t, a[0], i), s = o.top, c = o.right, l = o.bottom, u = o.left;
	for (let e = 1; e < a.length; e++) {
		let n = na(t, a[e], i);
		s = kr(n.top, s), c = Or(n.right, c), l = Or(n.bottom, l), u = kr(n.left, u);
	}
	return {
		width: c - u,
		height: l - s,
		x: u,
		y: s
	};
}
function aa(e) {
	let { width: t, height: n } = Vi(e);
	return {
		width: t,
		height: n
	};
}
function oa(e, t, n) {
	let r = Si(t), i = yi(t), a = n === "fixed", o = qi(e, !0, a, t), s = {
		scrollLeft: 0,
		scrollTop: 0
	}, c = Mr(0);
	if ((r || !a) && ((_i(t) !== "body" || wi(i)) && (s = Ii(t)), r)) {
		let e = qi(t, !0, a, t);
		c.x = e.x + t.clientLeft, c.y = e.y + t.clientTop;
	}
	!r && i && (c.x = Ji(i));
	let l = i && !r && !a ? Yi(i, s) : Mr(0);
	return {
		x: o.left + s.scrollLeft - c.x - l.x,
		y: o.top + s.scrollTop - c.y - l.y,
		width: o.width,
		height: o.height
	};
}
function sa(e) {
	return Fi(e).position === "static";
}
function ca(e, t) {
	if (!Si(e) || Fi(e).position === "fixed") return null;
	if (t) return t(e);
	let n = e.offsetParent;
	return yi(e) === n && (n = n.ownerDocument.body), n;
}
function la(e, t) {
	let n = vi(e);
	if (Ei(e)) return n;
	if (!Si(e)) {
		let t = Li(e);
		for (; t && !Pi(t);) {
			if (xi(t) && !sa(t)) return t;
			t = Li(t);
		}
		return n;
	}
	let r = ca(e, t);
	for (; r && Ti(r) && sa(r);) r = ca(r, t);
	return r && Pi(r) && sa(r) && !ji(r) ? n : r || Mi(e) || n;
}
var P = async function(e) {
	let t = this.getOffsetParent || la, n = this.getDimensions, r = await n(e.floating);
	return {
		reference: oa(e.reference, await t(e.floating), e.strategy),
		floating: {
			x: 0,
			y: 0,
			width: r.width,
			height: r.height
		}
	};
};
function F(e) {
	return Fi(e).direction === "rtl";
}
var ua = {
	convertOffsetParentRelativeRectToViewportRelativeRect: Xi,
	getDocumentElement: yi,
	getClippingRect: ia,
	getOffsetParent: la,
	getElementRects: P,
	getClientRects: Zi,
	getDimensions: aa,
	getScale: Ui,
	isElement: xi,
	isRTL: F
};
function da(e, t) {
	return e.x === t.x && e.y === t.y && e.width === t.width && e.height === t.height;
}
function fa(e, t, n) {
	let r = null, i, a = yi(e);
	function o() {
		var e;
		clearTimeout(i), (e = r) == null || e.disconnect(), r = null;
	}
	function s(n, c) {
		n === void 0 && (n = !1), c === void 0 && (c = 1), o();
		let l = e.getBoundingClientRect(), { left: u, top: d, width: f, height: p } = l;
		if (n || t(), !f || !p) return;
		let m = jr(d), h = jr(a.clientWidth - (u + f)), g = jr(a.clientHeight - (d + p)), _ = jr(u), v = {
			rootMargin: -m + "px " + -h + "px " + -g + "px " + -_ + "px",
			threshold: kr(0, Or(1, c)) || 1
		}, y = !0;
		function b(t) {
			let n = t[0].intersectionRatio;
			if (!da(l, e.getBoundingClientRect())) return s();
			if (n !== c) {
				if (!y) return s();
				n ? s(!1, n) : i = setTimeout(() => {
					s(!1, 1e-7);
				}, 1e3);
			}
			y = !1;
		}
		try {
			r = new IntersectionObserver(b, {
				...v,
				root: a.ownerDocument
			});
		} catch {
			r = new IntersectionObserver(b, v);
		}
		r.observe(e);
	}
	let c = vi(e), l = () => s(n);
	return c.addEventListener("resize", l), s(!0), () => {
		c.removeEventListener("resize", l), o();
	};
}
function pa(e, t, n, r) {
	r === void 0 && (r = {});
	let { ancestorScroll: i = !0, ancestorResize: a = !0, elementResize: o = typeof ResizeObserver == "function", layoutShift: s = typeof IntersectionObserver == "function", animationFrame: c = !1 } = r, l = Hi(e), u = i || a ? [...l ? zi(l) : [], ...t ? zi(t) : []] : [];
	u.forEach((e) => {
		i && e.addEventListener("scroll", n), a && e.addEventListener("resize", n);
	});
	let d = l && s ? fa(l, n, a) : null, f = -1, p = null;
	o && (p = new ResizeObserver((e) => {
		let [r] = e;
		r && r.target === l && p && t && (p.unobserve(t), cancelAnimationFrame(f), f = requestAnimationFrame(() => {
			var e;
			(e = p) == null || e.observe(t);
		})), n();
	}), l && !c && p.observe(l), t && p.observe(t));
	let m, h = c ? qi(e) : null;
	c && g();
	function g() {
		let t = qi(e);
		h && !da(h, t) && n(), h = t, m = requestAnimationFrame(g);
	}
	return n(), () => {
		var e;
		u.forEach((e) => {
			i && e.removeEventListener("scroll", n), a && e.removeEventListener("resize", n);
		}), d?.(), (e = p) == null || e.disconnect(), p = null, c && cancelAnimationFrame(m);
	};
}
var ma = fi, ha = pi, ga = oi, _a = hi, va = li, ya = ai, ba = mi, xa = (e, t, n) => {
	let r = /* @__PURE__ */ new Map(), i = n ?? {}, a = {
		...ua,
		...i.platform,
		_c: r
	};
	return ii(e, t, {
		...i,
		platform: a
	});
}, Sa = typeof document < "u" ? _.useLayoutEffect : function() {};
function Ca(e, t) {
	if (e === t) return !0;
	if (typeof e != typeof t) return !1;
	if (typeof e == "function" && e.toString() === t.toString()) return !0;
	let n, r, i;
	if (e && t && typeof e == "object") {
		if (Array.isArray(e)) {
			if (n = e.length, n !== t.length) return !1;
			for (r = n; r-- !== 0;) if (!Ca(e[r], t[r])) return !1;
			return !0;
		}
		if (i = Object.keys(e), n = i.length, n !== Object.keys(t).length) return !1;
		for (r = n; r-- !== 0;) if (!{}.hasOwnProperty.call(t, i[r])) return !1;
		for (r = n; r-- !== 0;) {
			let n = i[r];
			if (!(n === "_owner" && e.$$typeof) && !Ca(e[n], t[n])) return !1;
		}
		return !0;
	}
	return e !== e && t !== t;
}
function wa(e) {
	return typeof window > "u" ? 1 : (e.ownerDocument.defaultView || window).devicePixelRatio || 1;
}
function Ta(e, t) {
	let n = wa(e);
	return Math.round(t * n) / n;
}
function Ea(e) {
	let t = _.useRef(e);
	return Sa(() => {
		t.current = e;
	}), t;
}
function Da(e) {
	e === void 0 && (e = {});
	let { placement: t = "bottom", strategy: n = "absolute", middleware: r = [], platform: i, elements: { reference: a, floating: o } = {}, transform: s = !0, whileElementsMounted: c, open: l } = e, [u, d] = _.useState({
		x: 0,
		y: 0,
		strategy: n,
		placement: t,
		middlewareData: {},
		isPositioned: !1
	}), [f, p] = _.useState(r);
	Ca(f, r) || p(r);
	let [m, h] = _.useState(null), [g, v] = _.useState(null), y = _.useCallback((e) => {
		e !== C.current && (C.current = e, h(e));
	}, []), b = _.useCallback((e) => {
		e !== w.current && (w.current = e, v(e));
	}, []), x = a || m, S = o || g, C = _.useRef(null), w = _.useRef(null), T = _.useRef(u), ee = c != null, E = Ea(c), te = Ea(i), ne = Ea(l), re = _.useCallback(() => {
		if (!C.current || !w.current) return;
		let e = {
			placement: t,
			strategy: n,
			middleware: f
		};
		te.current && (e.platform = te.current), xa(C.current, w.current, e).then((e) => {
			let t = {
				...e,
				isPositioned: ne.current !== !1
			};
			D.current && !Ca(T.current, t) && (T.current = t, he.flushSync(() => {
				d(t);
			}));
		});
	}, [
		f,
		t,
		n,
		te,
		ne
	]);
	Sa(() => {
		l === !1 && T.current.isPositioned && (T.current.isPositioned = !1, d((e) => ({
			...e,
			isPositioned: !1
		})));
	}, [l]);
	let D = _.useRef(!1);
	Sa(() => (D.current = !0, () => {
		D.current = !1;
	}), []), Sa(() => {
		if (x && (C.current = x), S && (w.current = S), x && S) {
			if (E.current) return E.current(x, S, re);
			re();
		}
	}, [
		x,
		S,
		re,
		E,
		ee
	]);
	let ie = _.useMemo(() => ({
		reference: C,
		floating: w,
		setReference: y,
		setFloating: b
	}), [y, b]), ae = _.useMemo(() => ({
		reference: x,
		floating: S
	}), [x, S]), oe = _.useMemo(() => {
		let e = {
			position: n,
			left: 0,
			top: 0
		};
		if (!ae.floating) return e;
		let t = Ta(ae.floating, u.x), r = Ta(ae.floating, u.y);
		return s ? {
			...e,
			transform: "translate(" + t + "px, " + r + "px)",
			...wa(ae.floating) >= 1.5 && { willChange: "transform" }
		} : {
			position: n,
			left: t,
			top: r
		};
	}, [
		n,
		s,
		ae.floating,
		u.x,
		u.y
	]);
	return _.useMemo(() => ({
		...u,
		update: re,
		refs: ie,
		elements: ae,
		floatingStyles: oe
	}), [
		u,
		re,
		ie,
		ae,
		oe
	]);
}
var Oa = (e) => {
	function t(e) {
		return {}.hasOwnProperty.call(e, "current");
	}
	return {
		name: "arrow",
		options: e,
		fn(n) {
			let { element: r, padding: i } = typeof e == "function" ? e(n) : e;
			return r && t(r) ? r.current == null ? {} : ya({
				element: r.current,
				padding: i
			}).fn(n) : r ? ya({
				element: r,
				padding: i
			}).fn(n) : {};
		}
	};
}, ka = (e, t) => {
	let n = ma(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, Aa = (e, t) => {
	let n = ha(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, ja = (e, t) => ({
	fn: ba(e).fn,
	options: [e, t]
}), Ma = (e, t) => {
	let n = ga(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, Na = (e, t) => {
	let n = _a(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, Pa = (e, t) => {
	let n = va(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, Fa = (e, t) => {
	let n = Oa(e);
	return {
		name: n.name,
		fn: n.fn,
		options: [e, t]
	};
}, Ia = Object.defineProperty, La = (e, t) => Ia(e, "name", {
	value: t,
	configurable: !0
}), Ra = {
	Partial: "partial",
	Always: "always"
}, za = {
	Optimized: "optimized",
	Always: "always"
}, Ba = "Popper", [Va, Ha] = /* @__PURE__ */ Te(Ba), [Ua, Wa] = Va(Ba), Ga = /* @__PURE__ */ La((e) => {
	let { __scopePopper: t, children: n } = e, [r, i] = _.useState(null), [a, o] = _.useState(void 0);
	return /* @__PURE__ */ (0, O.jsx)(Ua, {
		scope: t,
		anchor: r,
		onAnchorChange: i,
		placementState: a,
		setPlacementState: o,
		children: n
	});
}, "Popper"), Ka = "PopperAnchor", qa = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ La(function(e, t) {
	let { __scopePopper: n, virtualRef: r, ...i } = e, a = Wa(Ka, n), o = _.useRef(null), s = a.onAnchorChange, c = S(t, _.useCallback((e) => {
		o.current = e, e && s(e);
	}, [s])), l = _.useRef(null);
	_.useEffect(() => {
		if (!r) return;
		let e = l.current;
		l.current = r.current, e !== l.current && s(l.current);
	});
	let u = a.placementState && eo(a.placementState), d = u?.[0], f = u?.[1];
	return r ? null : /* @__PURE__ */ (0, O.jsx)(ve.div, {
		"data-radix-popper-side": d,
		"data-radix-popper-align": f,
		...i,
		ref: c
	});
}, "PopperAnchor")), Ja = "PopperContent", [Ya, Xa] = Va(Ja), Za = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ La(function(e, t) {
	let { __scopePopper: n, side: r = "bottom", sideOffset: i = 0, align: a = "center", alignOffset: o = 0, arrowPadding: s = 0, avoidCollisions: c = !0, collisionBoundary: l = [], collisionPadding: u = 0, sticky: d = Ra.Partial, hideWhenDetached: f = !1, updatePositionStrategy: p = za.Optimized, onPlaced: m, ...h } = e, g = Wa(Ja, n), [v, y] = _.useState(null), b = S(t, y), [x, C] = _.useState(null), w = Er(x), T = w?.width ?? 0, ee = w?.height ?? 0, E = r + (a === "center" ? "" : "-" + a), te = typeof u == "number" ? u : {
		top: 0,
		right: 0,
		bottom: 0,
		left: 0,
		...u
	}, ne = Array.isArray(l) ? l : [l], re = ne.length > 0, D = {
		padding: te,
		boundary: ne.filter(Qa),
		altBoundary: re
	}, { refs: ie, floatingStyles: ae, placement: oe, isPositioned: se, middlewareData: ce } = Da({
		strategy: "fixed",
		placement: E,
		whileElementsMounted: /* @__PURE__ */ La((...e) => pa(...e, { animationFrame: p === za.Always }), "whileElementsMounted"),
		elements: { reference: g.anchor },
		middleware: [
			ka({
				mainAxis: i + ee,
				alignmentAxis: o
			}),
			c && Aa({
				mainAxis: !0,
				crossAxis: !1,
				...d === Ra.Partial ? { limiter: ja() } : {},
				...D
			}),
			c && Ma({ ...D }),
			Na({
				...D,
				apply: /* @__PURE__ */ La(({ elements: e, rects: t, availableWidth: n, availableHeight: r }) => {
					let { width: i, height: a } = t.reference, o = e.floating.style;
					o.setProperty("--radix-popper-available-width", `${n}px`), o.setProperty("--radix-popper-available-height", `${r}px`), o.setProperty("--radix-popper-anchor-width", `${i}px`), o.setProperty("--radix-popper-anchor-height", `${a}px`);
				}, "apply")
			}),
			x && Fa({
				element: x,
				padding: s
			}),
			$a({
				arrowWidth: T,
				arrowHeight: ee
			}),
			f && Pa(re ? {
				strategy: "referenceHidden",
				...D
			} : {
				strategy: "referenceHidden",
				padding: D.padding,
				altBoundary: D.altBoundary
			})
		]
	}), le = g.setPlacementState;
	j(() => (le(oe), () => {
		le(void 0);
	}), [oe, le]);
	let [ue, de] = eo(oe), fe = _t(m);
	j(() => {
		se && fe?.();
	}, [se, fe]);
	let pe = ce.arrow?.x, me = ce.arrow?.y, he = ce.arrow?.centerOffset !== 0, [ge, _e] = _.useState();
	return j(() => {
		v && _e(window.getComputedStyle(v).zIndex);
	}, [v]), /* @__PURE__ */ (0, O.jsx)("div", {
		ref: ie.setFloating,
		"data-radix-popper-content-wrapper": "",
		style: {
			...ae,
			transform: se ? ae.transform : "translate(0, -200%)",
			minWidth: "max-content",
			zIndex: ge,
			"--radix-popper-transform-origin": [ce.transformOrigin?.x, ce.transformOrigin?.y].join(" "),
			...ce.hide?.referenceHidden && {
				visibility: "hidden",
				pointerEvents: "none"
			}
		},
		dir: e.dir,
		children: /* @__PURE__ */ (0, O.jsx)(Ya, {
			scope: n,
			placedSide: ue,
			placedAlign: de,
			onArrowChange: C,
			arrowX: pe,
			arrowY: me,
			shouldHideArrow: he,
			children: /* @__PURE__ */ (0, O.jsx)(ve.div, {
				"data-side": ue,
				"data-align": de,
				...h,
				ref: b,
				style: {
					...h.style,
					animation: se ? h.style?.animation : "none"
				}
			})
		})
	});
}, "PopperContent"));
function Qa(e) {
	return e !== null;
}
La(Qa, "isNotNull");
var $a = /* @__PURE__ */ La((e) => ({
	name: "transformOrigin",
	options: e,
	fn(t) {
		let { placement: n, rects: r, middlewareData: i } = t, a = i.arrow?.centerOffset !== 0, o = a ? 0 : e.arrowWidth, s = a ? 0 : e.arrowHeight, [c, l] = eo(n), u = {
			start: "0%",
			center: "50%",
			end: "100%"
		}[l], d = (i.arrow?.x ?? 0) + o / 2, f = (i.arrow?.y ?? 0) + s / 2, p = "", m = "";
		return c === "bottom" ? (p = a ? u : `${d}px`, m = `${-s}px`) : c === "top" ? (p = a ? u : `${d}px`, m = `${r.floating.height + s}px`) : c === "right" ? (p = `${-s}px`, m = a ? u : `${f}px`) : c === "left" && (p = `${r.floating.width + s}px`, m = a ? u : `${f}px`), { data: {
			x: p,
			y: m
		} };
	}
}), "transformOrigin");
function eo(e) {
	let [t, n = "center"] = e.split("-");
	return [t, n];
}
La(eo, "getSideAndAlignFromPlacement");
// node_modules/@radix-ui/react-popover/dist/index.mjs
var to = Object.defineProperty, no = (e, t) => to(e, "name", {
	value: t,
	configurable: !0
}), ro = "Popover", [io, ao] = /* @__PURE__ */ Te(ro, [Ha]), oo = Ha(), [so, co] = io(ro), lo = /* @__PURE__ */ no((e) => {
	let { __scopePopover: t, children: n, open: r, defaultOpen: i, onOpenChange: a, modal: o = !1 } = e, s = oo(t), c = _.useRef(null), [l, u] = _.useState(!1), [d, f] = _.useState(0), [p, m] = _.useState(0), [h, g] = Ue({
		prop: r,
		defaultProp: i ?? !1,
		onChange: a,
		caller: ro
	});
	return /* @__PURE__ */ (0, O.jsx)(Ga, {
		...s,
		children: /* @__PURE__ */ (0, O.jsx)(so, {
			scope: t,
			contentId: st(),
			titleId: st(),
			descriptionId: st(),
			titlePresent: d > 0,
			descriptionPresent: p > 0,
			setTitleCount: f,
			setDescriptionCount: m,
			triggerRef: c,
			open: h,
			onOpenChange: g,
			onOpenToggle: _.useCallback(() => g((e) => !e), [g]),
			hasCustomAnchor: l,
			onCustomAnchorAdd: _.useCallback(() => u(!0), []),
			onCustomAnchorRemove: _.useCallback(() => u(!1), []),
			modal: o,
			children: n
		})
	});
}, "Popover"), uo = "PopoverAnchor", fo = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let { __scopePopover: n, ...r } = e, i = co(uo, n), a = oo(n), { onCustomAnchorAdd: o, onCustomAnchorRemove: s } = i;
	return _.useEffect(() => (o(), () => s()), [o, s]), /* @__PURE__ */ (0, O.jsx)(qa, {
		...a,
		...r,
		ref: t
	});
}, "PopoverAnchor")), po = "PopoverTrigger", mo = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let { __scopePopover: n, ...r } = e, i = co(po, n), a = oo(n), o = S(t, i.triggerRef), s = /* @__PURE__ */ (0, O.jsx)(ve.button, {
		type: "button",
		"aria-haspopup": "dialog",
		"aria-expanded": i.open,
		"aria-controls": i.open ? i.contentId : void 0,
		"data-state": Do(i.open),
		...r,
		ref: o,
		onClick: Ae(e.onClick, i.onOpenToggle)
	});
	return i.hasCustomAnchor ? s : /* @__PURE__ */ (0, O.jsx)(qa, {
		asChild: !0,
		...a,
		children: s
	});
}, "PopoverTrigger")), ho = "PopoverPortal", [go, _o] = io(ho, { forceMount: void 0 }), vo = /* @__PURE__ */ no((e) => {
	let { __scopePopover: t, forceMount: n, children: r, container: i } = e, a = co(ho, t);
	return /* @__PURE__ */ (0, O.jsx)(go, {
		scope: t,
		forceMount: n,
		children: /* @__PURE__ */ (0, O.jsx)(Ze, {
			present: n || a.open,
			children: /* @__PURE__ */ (0, O.jsx)(Qt, {
				asChild: !0,
				container: i,
				children: r
			})
		})
	});
}, "PopoverPortal"), yo = "PopoverContent", bo = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let n = _o(yo, e.__scopePopover), { forceMount: r = n.forceMount, ...i } = e, a = co(yo, e.__scopePopover);
	return /* @__PURE__ */ (0, O.jsx)(Ze, {
		present: r || a.open,
		children: a.modal ? /* @__PURE__ */ (0, O.jsx)(So, {
			...i,
			ref: t
		}) : /* @__PURE__ */ (0, O.jsx)(Co, {
			...i,
			ref: t
		})
	});
}, "PopoverContent")), xo = /* @__PURE__ */ T("PopoverContent.RemoveScroll"), So = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let n = co(yo, e.__scopePopover), r = _.useRef(null), i = S(t, r), a = _.useRef(!1), { nodes: o, registry: s } = zt();
	return _.useEffect(() => {
		let e = r.current;
		if (e) return Cr(e);
	}, []), /* @__PURE__ */ (0, O.jsx)(mr, {
		as: xo,
		allowPinchZoom: !0,
		shards: _.useMemo(() => [r, ...o.map((e) => ({ current: e }))], [r, o]),
		children: /* @__PURE__ */ (0, O.jsx)(wo, {
			...e,
			ref: i,
			branchNodes: o,
			branchRegistry: s,
			trapFocus: n.open,
			disableOutsidePointerEvents: !0,
			onCloseAutoFocus: Ae(e.onCloseAutoFocus, (e) => {
				e.preventDefault(), a.current || n.triggerRef.current?.focus();
			}),
			onPointerDownOutside: Ae(e.onPointerDownOutside, (e) => {
				let t = e.detail.originalEvent, n = t.button === 0 && t.ctrlKey === !0, r = t.button === 2 || n;
				a.current = r;
			}, { checkForDefaultPrevented: !1 }),
			onFocusOutside: Ae(e.onFocusOutside, (e) => e.preventDefault(), { checkForDefaultPrevented: !1 })
		})
	});
}, "PopoverContentModal")), Co = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let n = co(yo, e.__scopePopover), r = _.useRef(!1), i = _.useRef(!1);
	return /* @__PURE__ */ (0, O.jsx)(wo, {
		...e,
		ref: t,
		trapFocus: !1,
		disableOutsidePointerEvents: !1,
		onCloseAutoFocus: (t) => {
			e.onCloseAutoFocus?.(t), t.defaultPrevented || (r.current || n.triggerRef.current?.focus(), t.preventDefault()), r.current = !1, i.current = !1;
		},
		onInteractOutside: (t) => {
			e.onInteractOutside?.(t), t.defaultPrevented || (r.current = !0, t.detail.originalEvent.type === "pointerdown" && (i.current = !0));
			let a = t.target;
			n.triggerRef.current?.contains(a) && t.preventDefault(), t.detail.originalEvent.type === "focusin" && i.current && t.preventDefault();
		}
	});
}, "PopoverContentNonModal")), wo = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let { __scopePopover: n, trapFocus: r, onOpenAutoFocus: i, onCloseAutoFocus: a, disableOutsidePointerEvents: o, onEscapeKeyDown: s, onPointerDownOutside: c, onFocusOutside: l, onInteractOutside: u, "aria-describedby": d, branchNodes: f, branchRegistry: p, ...m } = e, h = co(yo, n), g = oo(n), [v, y] = _.useState(null), b = S(t, y);
	return Bt(v), an(), /* @__PURE__ */ (0, O.jsx)(Rt, {
		registry: p,
		children: /* @__PURE__ */ (0, O.jsx)(It, {
			asChild: !0,
			loop: !0,
			trapped: r,
			branches: f,
			onMountAutoFocus: i,
			onUnmountAutoFocus: a,
			children: /* @__PURE__ */ (0, O.jsx)(wt, {
				asChild: !0,
				disableOutsidePointerEvents: o,
				onInteractOutside: u,
				onEscapeKeyDown: s,
				onPointerDownOutside: c,
				onFocusOutside: l,
				onDismiss: () => h.onOpenChange(!1),
				deferPointerDownOutside: !0,
				children: /* @__PURE__ */ (0, O.jsx)(Za, {
					"data-state": Do(h.open),
					role: "dialog",
					id: h.contentId,
					"aria-labelledby": h.titlePresent ? h.titleId : void 0,
					"aria-describedby": h.descriptionPresent ? Oo(d, h.descriptionId) : d,
					...g,
					...m,
					ref: b,
					style: {
						...m.style,
						"--radix-popover-content-transform-origin": "var(--radix-popper-transform-origin)",
						"--radix-popover-content-available-width": "var(--radix-popper-available-width)",
						"--radix-popover-content-available-height": "var(--radix-popper-available-height)",
						"--radix-popover-trigger-width": "var(--radix-popper-anchor-width)",
						"--radix-popover-trigger-height": "var(--radix-popper-anchor-height)"
					}
				})
			})
		})
	});
}, "PopoverContentImpl")), To = "PopoverClose", Eo = /* @__PURE__ */ _.forwardRef(/* @__PURE__ */ no(function(e, t) {
	let { __scopePopover: n, ...r } = e, i = co(To, n);
	return /* @__PURE__ */ (0, O.jsx)(ve.button, {
		type: "button",
		...r,
		ref: t,
		onClick: Ae(e.onClick, () => i.onOpenChange(!1))
	});
}, "PopoverClose"));
function Do(e) {
	return e ? "open" : "closed";
}
no(Do, "getState");
function Oo(...e) {
	let t = /* @__PURE__ */ new Set();
	for (let n of e) if (typeof n == "string") for (let e of String(n).trim().split(/\s+/)) e && t.add(e);
	return t.size > 0 ? Array.from(t).join(" ") : void 0;
}
no(Oo, "concatAriaDescribedby");
// node_modules/@radix-ui/react-tooltip/dist/index.mjs
var ko = Object.defineProperty, Ao = (e, t) => ko(e, "name", {
	value: t,
	configurable: !0
}), [jo, Mo] = /* @__PURE__ */ Te("Tooltip", [Ha]);
Ha();
var No = "TooltipProvider", Po = 700, [Fo, Io] = jo(No), Lo = /* @__PURE__ */ Ao((e) => {
	let { __scopeTooltip: t, delayDuration: n = Po, skipDelayDuration: r = 300, disableHoverableContent: i = !1, children: a } = e, o = _.useRef(!0), s = _.useRef(!1), c = _.useRef(0);
	return _.useEffect(() => {
		let e = c.current;
		return () => window.clearTimeout(e);
	}, []), /* @__PURE__ */ (0, O.jsx)(Fo, {
		scope: t,
		isOpenDelayedRef: o,
		delayDuration: n,
		onOpen: _.useCallback(() => {
			r <= 0 || (window.clearTimeout(c.current), o.current = !1);
		}, [r]),
		onClose: _.useCallback(() => {
			r <= 0 || (window.clearTimeout(c.current), c.current = window.setTimeout(() => o.current = !0, r));
		}, [r]),
		isPointerInTransitRef: s,
		onPointerInTransitChange: _.useCallback((e) => {
			s.current = e;
		}, []),
		disableHoverableContent: i,
		children: a
	});
}, "TooltipProvider"), [Ro, zo] = jo("Tooltip"), [Bo, Vo] = jo("TooltipPortal", { forceMount: void 0 });
function Ho(e, t) {
	let n = Math.abs(t.top - e.y), r = Math.abs(t.bottom - e.y), i = Math.abs(t.right - e.x), a = Math.abs(t.left - e.x);
	switch (Math.min(n, r, i, a)) {
		case a: return "left";
		case i: return "right";
		case n: return "top";
		case r: return "bottom";
		default: throw Error("unreachable");
	}
}
Ao(Ho, "getExitSideFromRect");
function Uo(e, t, n = 5) {
	let r = [];
	switch (t) {
		case "top":
			r.push({
				x: e.x - n,
				y: e.y + n
			}, {
				x: e.x + n,
				y: e.y + n
			});
			break;
		case "bottom":
			r.push({
				x: e.x - n,
				y: e.y - n
			}, {
				x: e.x + n,
				y: e.y - n
			});
			break;
		case "left":
			r.push({
				x: e.x + n,
				y: e.y - n
			}, {
				x: e.x + n,
				y: e.y + n
			});
			break;
		case "right": r.push({
			x: e.x - n,
			y: e.y - n
		}, {
			x: e.x - n,
			y: e.y + n
		});
	}
	return r;
}
Ao(Uo, "getPaddedExitPoints");
function Wo(e) {
	let { top: t, right: n, bottom: r, left: i } = e;
	return [
		{
			x: i,
			y: t
		},
		{
			x: n,
			y: t
		},
		{
			x: n,
			y: r
		},
		{
			x: i,
			y: r
		}
	];
}
Ao(Wo, "getPointsFromRect");
function Go(e, t) {
	let { x: n, y: r } = e, i = !1;
	for (let e = 0, a = t.length - 1; e < t.length; a = e++) {
		let o = t[e], s = t[a], c = o.x, l = o.y, u = s.x, d = s.y;
		l > r != d > r && n < (u - c) * (r - l) / (d - l) + c && (i = !i);
	}
	return i;
}
Ao(Go, "isPointInPolygon");
function Ko(e) {
	let t = e.slice();
	return t.sort((e, t) => e.x < t.x ? -1 : e.x > t.x ? 1 : e.y < t.y ? -1 : +(e.y > t.y)), qo(t);
}
Ao(Ko, "getHull");
function qo(e) {
	if (e.length <= 1) return e.slice();
	let t = [];
	for (let n = 0; n < e.length; n++) {
		let r = e[n];
		for (; t.length >= 2;) {
			let e = t[t.length - 1], n = t[t.length - 2];
			if ((e.x - n.x) * (r.y - n.y) >= (e.y - n.y) * (r.x - n.x)) t.pop();
			else break;
		}
		t.push(r);
	}
	t.pop();
	let n = [];
	for (let t = e.length - 1; t >= 0; t--) {
		let r = e[t];
		for (; n.length >= 2;) {
			let e = n[n.length - 1], t = n[n.length - 2];
			if ((e.x - t.x) * (r.y - t.y) >= (e.y - t.y) * (r.x - t.x)) n.pop();
			else break;
		}
		n.push(r);
	}
	return n.pop(), t.length === 1 && n.length === 1 && t[0].x === n[0].x && t[0].y === n[0].y ? t : t.concat(n);
}
Ao(qo, "getHullPresorted");
function I(...e) {
	let t = /* @__PURE__ */ new Set();
	for (let n of e) if (typeof n == "string") for (let e of String(n).trim().split(/\s+/)) e && t.add(e);
	return t.size > 0 ? Array.from(t).join(" ") : void 0;
}
Ao(I, "concatAriaDescribedby");
// node_modules/classnames/index.js
var L = /* @__PURE__ */ o(((e, t) => {
	(function() {
		var e = {}.hasOwnProperty;
		function n() {
			for (var e = "", t = 0; t < arguments.length; t++) {
				var n = arguments[t];
				n && (e = i(e, r(n)));
			}
			return e;
		}
		function r(t) {
			if (typeof t == "string" || typeof t == "number") return t;
			if (typeof t != "object") return "";
			if (Array.isArray(t)) return n.apply(null, t);
			if (t.toString !== Object.prototype.toString && !t.toString.toString().includes("[native code]")) return t.toString();
			var r = "";
			for (var a in t) e.call(t, a) && t[a] && (r = i(r, a));
			return r;
		}
		function i(e, t) {
			return t ? e ? e + " " + t : e + t : e;
		}
		t !== void 0 && t.exports ? (n.default = n, t.exports = n) : typeof define == "function" && typeof define.amd == "object" && define.amd ? define("classnames", [], function() {
			return n;
		}) : window.classNames = n;
	})();
})), R = { asChild: { type: "boolean" } }, Jo = {
	width: {
		type: "string",
		className: "rt-r-w",
		customProperties: ["--width"],
		responsive: !0
	},
	minWidth: {
		type: "string",
		className: "rt-r-min-w",
		customProperties: ["--min-width"],
		responsive: !0
	},
	maxWidth: {
		type: "string",
		className: "rt-r-max-w",
		customProperties: ["--max-width"],
		responsive: !0
	}
}, Yo = {
	height: {
		type: "string",
		className: "rt-r-h",
		customProperties: ["--height"],
		responsive: !0
	},
	minHeight: {
		type: "string",
		className: "rt-r-min-h",
		customProperties: ["--min-height"],
		responsive: !0
	},
	maxHeight: {
		type: "string",
		className: "rt-r-max-h",
		customProperties: ["--max-height"],
		responsive: !0
	}
}, Xo = /* @__PURE__ */ "gray.gold.bronze.brown.yellow.amber.orange.tomato.red.ruby.crimson.pink.plum.purple.violet.iris.indigo.blue.cyan.teal.jade.green.grass.lime.mint.sky".split("."), Zo = [
	"auto",
	"gray",
	"mauve",
	"slate",
	"sage",
	"olive",
	"sand"
], Qo = { color: {
	type: "enum",
	values: Xo,
	default: void 0
} }, $o = { color: {
	type: "enum",
	values: Xo,
	default: ""
} }, es = { highContrast: {
	type: "boolean",
	className: "rt-high-contrast",
	default: void 0
} }, z = { trim: {
	type: "enum",
	className: "rt-r-lt",
	values: [
		"normal",
		"start",
		"end",
		"both"
	],
	responsive: !0
} }, ts = { align: {
	type: "enum",
	className: "rt-r-ta",
	values: [
		"left",
		"center",
		"right"
	],
	responsive: !0
} }, ns = { wrap: {
	type: "enum",
	className: "rt-r-tw",
	values: [
		"wrap",
		"nowrap",
		"pretty",
		"balance"
	],
	responsive: !0
} }, rs = { truncate: {
	type: "boolean",
	className: "rt-truncate"
} }, is = { weight: {
	type: "enum",
	className: "rt-r-weight",
	values: [
		"light",
		"regular",
		"medium",
		"bold"
	],
	responsive: !0
} }, as = /* @__PURE__ */ new Set([
	"initial",
	"xs",
	"sm",
	"md",
	"lg",
	"xl"
]);
// node_modules/@radix-ui/themes/dist/esm/helpers/has-own-property.js
function os(e, t) {
	return Object.prototype.hasOwnProperty.call(e, t);
}
// node_modules/@radix-ui/themes/dist/esm/helpers/is-responsive-object.js
function ss(e) {
	return typeof e == "object" && !!e && Object.keys(e).some((e) => as.has(e));
}
// node_modules/@radix-ui/themes/dist/esm/helpers/get-responsive-styles.js
function cs({ className: e, customProperties: t, ...n }) {
	return [ls({
		allowArbitraryValues: !0,
		className: e,
		...n
	}), us({
		customProperties: t,
		...n
	})];
}
function ls({ allowArbitraryValues: e, value: t, className: n, propValues: r, parseValue: i = (e) => e }) {
	let a = [];
	if (t) {
		if (typeof t == "string" && r.includes(t)) return B(n, t, i);
		if (ss(t)) {
			let o = t;
			for (let t in o) {
				if (!os(o, t) || !as.has(t)) continue;
				let s = o[t];
				if (s !== void 0) {
					if (r.includes(s)) {
						let e = B(n, s, i), r = t === "initial" ? e : `${t}:${e}`;
						a.push(r);
					} else if (e) {
						let e = t === "initial" ? n : `${t}:${n}`;
						a.push(e);
					}
				}
			}
			return a.join(" ");
		}
		if (e) return n;
	}
}
function B(e, t, n) {
	let r = e ? "-" : "", i = n(t), a = i?.startsWith("-");
	return `${a ? "-" : ""}${e}${r}${a ? i?.substring(1) : i}`;
}
function us({ customProperties: e, value: t, propValues: n, parseValue: r = (e) => e }) {
	let i = {};
	if (!(!t || typeof t == "string" && n.includes(t))) {
		if (typeof t == "string" && (i = Object.fromEntries(e.map((e) => [e, t]))), ss(t)) {
			let r = t;
			for (let t in r) {
				if (!os(r, t) || !as.has(t)) continue;
				let a = r[t];
				if (!n.includes(a)) for (let n of e) i = {
					[t === "initial" ? n : `${n}-${t}`]: a,
					...i
				};
			}
		}
		for (let e in i) {
			let t = i[e];
			t !== void 0 && (i[e] = r(t));
		}
		return i;
	}
}
// node_modules/@radix-ui/themes/dist/esm/helpers/merge-styles.js
function ds(...e) {
	let t = {};
	for (let n of e) n && (t = {
		...t,
		...n
	});
	return Object.keys(t).length ? t : void 0;
}
// node_modules/@radix-ui/themes/dist/esm/helpers/extract-props.js
var fs = /* @__PURE__ */ c(L());
function ps(...e) {
	return Object.assign({}, ...e);
}
function ms(e, ...t) {
	let n, r, i = { ...e }, a = ps(...t);
	for (let e in a) {
		let t = i[e], o = a[e];
		if (o.default !== void 0 && t === void 0 && (t = o.default), o.type === "enum" && ![o.default, ...o.values].includes(t) && !ss(t) && (t = o.default), i[e] = t, "className" in o && o.className) {
			delete i[e];
			let a = "responsive" in o;
			if (!t || ss(t) && !a) continue;
			if (ss(t) && (o.default !== void 0 && t.initial === void 0 && (t.initial = o.default), o.type === "enum" && ([o.default, ...o.values].includes(t.initial) || (t.initial = o.default))), o.type === "enum") {
				let e = ls({
					allowArbitraryValues: !1,
					value: t,
					className: o.className,
					propValues: o.values,
					parseValue: o.parseValue
				});
				n = (0, fs.default)(n, e);
				continue;
			}
			if (o.type === "string" || o.type === "enum | string") {
				let e = o.type === "string" ? [] : o.values, [i, a] = cs({
					className: o.className,
					customProperties: o.customProperties,
					propValues: e,
					parseValue: o.parseValue,
					value: t
				});
				r = ds(r, a), n = (0, fs.default)(n, i);
				continue;
			}
			if (o.type === "boolean" && t) {
				n = (0, fs.default)(n, o.className);
				continue;
			}
		}
	}
	return i.className = (0, fs.default)(n, e.className), i.style = ds(r, e.style), i;
}
// node_modules/@radix-ui/themes/dist/esm/props/margin.props.js
var hs = [
	"0",
	"1",
	"2",
	"3",
	"4",
	"5",
	"6",
	"7",
	"8",
	"9",
	"-1",
	"-2",
	"-3",
	"-4",
	"-5",
	"-6",
	"-7",
	"-8",
	"-9"
], gs = {
	m: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-m",
		customProperties: ["--m"]
	},
	mx: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-mx",
		customProperties: ["--ml", "--mr"]
	},
	my: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-my",
		customProperties: ["--mt", "--mb"]
	},
	mt: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-mt",
		customProperties: ["--mt"]
	},
	mr: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-mr",
		customProperties: ["--mr"]
	},
	mb: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-mb",
		customProperties: ["--mb"]
	},
	ml: {
		type: "enum | string",
		values: hs,
		responsive: !0,
		className: "rt-r-ml",
		customProperties: ["--ml"]
	}
}, _s = [
	"span",
	"div",
	"label",
	"p"
], vs = [
	"1",
	"2",
	"3",
	"4",
	"5",
	"6",
	"7",
	"8",
	"9"
], ys = {
	as: {
		type: "enum",
		values: _s,
		default: "span"
	},
	...R,
	size: {
		type: "enum",
		className: "rt-r-size",
		values: vs,
		responsive: !0
	},
	...is,
	...ts,
	...z,
	...rs,
	...ns,
	...Qo,
	...es
}, bs = _.forwardRef((e, t) => {
	let { children: n, className: r, asChild: i, as: a = "span", color: o, ...s } = ms(e, ys, gs);
	return _.createElement(ee, {
		"data-accent-color": o,
		...s,
		ref: t,
		className: (0, fs.default)("rt-Text", r)
	}, i ? n : _.createElement(a, null, n));
});
bs.displayName = "Text";
// node_modules/@radix-ui/themes/dist/esm/helpers/get-matching-gray-color.js
function xs(e) {
	switch (e) {
		case "tomato":
		case "red":
		case "ruby":
		case "crimson":
		case "pink":
		case "plum":
		case "purple":
		case "violet": return "mauve";
		case "iris":
		case "indigo":
		case "blue":
		case "sky":
		case "cyan": return "slate";
		case "teal":
		case "jade":
		case "mint":
		case "green": return "sage";
		case "grass":
		case "lime": return "olive";
		case "yellow":
		case "amber":
		case "orange":
		case "brown":
		case "gold":
		case "bronze": return "sand";
		case "gray": return "gray";
	}
}
// node_modules/@radix-ui/themes/dist/esm/props/radius.prop.js
var Ss = [
	"none",
	"small",
	"medium",
	"large",
	"full"
], Cs = { radius: {
	type: "enum",
	values: Ss,
	default: void 0
} }, ws = [
	"inherit",
	"light",
	"dark"
], Ts = ["solid", "translucent"], Es = [
	"90%",
	"95%",
	"100%",
	"105%",
	"110%"
], Ds = {
	...R,
	hasBackground: {
		type: "boolean",
		default: !0
	},
	appearance: {
		type: "enum",
		values: ws,
		default: "inherit"
	},
	accentColor: {
		type: "enum",
		values: Xo,
		default: "indigo"
	},
	grayColor: {
		type: "enum",
		values: Zo,
		default: "auto"
	},
	panelBackground: {
		type: "enum",
		values: Ts,
		default: "translucent"
	},
	radius: {
		type: "enum",
		values: Ss,
		default: "medium"
	},
	scaling: {
		type: "enum",
		values: Es,
		default: "100%"
	}
}, Os = () => {}, ks = _.createContext(void 0), As = _.forwardRef((e, t) => _.useContext(ks) === void 0 ? _.createElement(Lo, { delayDuration: 200 }, _.createElement(ft, { dir: "ltr" }, _.createElement(js, {
	...e,
	ref: t
}))) : _.createElement(Ms, {
	...e,
	ref: t
}));
As.displayName = "Theme";
var js = _.forwardRef((e, t) => {
	let { appearance: n = Ds.appearance.default, accentColor: r = Ds.accentColor.default, grayColor: i = Ds.grayColor.default, panelBackground: a = Ds.panelBackground.default, radius: o = Ds.radius.default, scaling: s = Ds.scaling.default, hasBackground: c = Ds.hasBackground.default, ...l } = e, [u, d] = _.useState(n);
	_.useEffect(() => d(n), [n]);
	let [f, p] = _.useState(r);
	_.useEffect(() => p(r), [r]);
	let [m, h] = _.useState(i);
	_.useEffect(() => h(i), [i]);
	let [g, v] = _.useState(a);
	_.useEffect(() => v(a), [a]);
	let [y, b] = _.useState(o);
	_.useEffect(() => b(o), [o]);
	let [x, S] = _.useState(s);
	return _.useEffect(() => S(s), [s]), _.createElement(Ms, {
		...l,
		ref: t,
		isRoot: !0,
		hasBackground: c,
		appearance: u,
		accentColor: f,
		grayColor: m,
		panelBackground: g,
		radius: y,
		scaling: x,
		onAppearanceChange: d,
		onAccentColorChange: p,
		onGrayColorChange: h,
		onPanelBackgroundChange: v,
		onRadiusChange: b,
		onScalingChange: S
	});
});
js.displayName = "ThemeRoot";
var Ms = _.forwardRef((e, t) => {
	let n = _.useContext(ks), { asChild: r, isRoot: i, hasBackground: a, appearance: o = n?.appearance ?? Ds.appearance.default, accentColor: s = n?.accentColor ?? Ds.accentColor.default, grayColor: c = n?.resolvedGrayColor ?? Ds.grayColor.default, panelBackground: l = n?.panelBackground ?? Ds.panelBackground.default, radius: u = n?.radius ?? Ds.radius.default, scaling: d = n?.scaling ?? Ds.scaling.default, onAppearanceChange: f = Os, onAccentColorChange: p = Os, onGrayColorChange: m = Os, onPanelBackgroundChange: h = Os, onRadiusChange: g = Os, onScalingChange: v = Os, ...y } = e, b = r ? ee : "div", x = c === "auto" ? xs(s) : c, S = e.appearance === "light" || e.appearance === "dark", C = a === void 0 ? i || S : a;
	return _.createElement(ks.Provider, { value: _.useMemo(() => ({
		appearance: o,
		accentColor: s,
		grayColor: c,
		resolvedGrayColor: x,
		panelBackground: l,
		radius: u,
		scaling: d,
		onAppearanceChange: f,
		onAccentColorChange: p,
		onGrayColorChange: m,
		onPanelBackgroundChange: h,
		onRadiusChange: g,
		onScalingChange: v
	}), [
		o,
		s,
		c,
		x,
		l,
		u,
		d,
		f,
		p,
		m,
		h,
		g,
		v
	]) }, _.createElement(b, {
		"data-is-root-theme": i ? "true" : "false",
		"data-accent-color": s,
		"data-gray-color": x,
		"data-has-background": C ? "true" : "false",
		"data-panel-background": l,
		"data-radius": u,
		"data-scaling": d,
		ref: t,
		...y,
		className: (0, fs.default)("radix-themes", {
			light: o === "light",
			dark: o === "dark"
		}, y.className)
	}));
});
Ms.displayName = "ThemeImpl";
// node_modules/@radix-ui/themes/dist/esm/helpers/require-react-element.js
var Ns = (e) => {
	if (!_.isValidElement(e)) throw Error(`Expected a single React Element child, but got: ${_.Children.toArray(e).map((e) => typeof e == "object" && "type" in e && typeof e.type == "string" ? e.type : typeof e).join(", ")}`);
	return e;
}, Ps = ee, Fs = [
	"0",
	"1",
	"2",
	"3",
	"4",
	"5",
	"6",
	"7",
	"8",
	"9"
], Is = {
	p: {
		type: "enum | string",
		className: "rt-r-p",
		customProperties: ["--p"],
		values: Fs,
		responsive: !0
	},
	px: {
		type: "enum | string",
		className: "rt-r-px",
		customProperties: ["--pl", "--pr"],
		values: Fs,
		responsive: !0
	},
	py: {
		type: "enum | string",
		className: "rt-r-py",
		customProperties: ["--pt", "--pb"],
		values: Fs,
		responsive: !0
	},
	pt: {
		type: "enum | string",
		className: "rt-r-pt",
		customProperties: ["--pt"],
		values: Fs,
		responsive: !0
	},
	pr: {
		type: "enum | string",
		className: "rt-r-pr",
		customProperties: ["--pr"],
		values: Fs,
		responsive: !0
	},
	pb: {
		type: "enum | string",
		className: "rt-r-pb",
		customProperties: ["--pb"],
		values: Fs,
		responsive: !0
	},
	pl: {
		type: "enum | string",
		className: "rt-r-pl",
		customProperties: ["--pl"],
		values: Fs,
		responsive: !0
	}
}, Ls = [
	"visible",
	"hidden",
	"clip",
	"scroll",
	"auto"
], Rs = [
	"static",
	"relative",
	"absolute",
	"fixed",
	"sticky"
], zs = [
	"0",
	"1",
	"2",
	"3",
	"4",
	"5",
	"6",
	"7",
	"8",
	"9",
	"-1",
	"-2",
	"-3",
	"-4",
	"-5",
	"-6",
	"-7",
	"-8",
	"-9"
], Bs = ["0", "1"], Vs = ["0", "1"], Hs = [
	"start",
	"center",
	"end",
	"baseline",
	"stretch"
], Us = [
	"start",
	"center",
	"end",
	"baseline",
	"stretch"
], Ws = {
	...Is,
	...Jo,
	...Yo,
	position: {
		type: "enum",
		className: "rt-r-position",
		values: Rs,
		responsive: !0
	},
	inset: {
		type: "enum | string",
		className: "rt-r-inset",
		customProperties: ["--inset"],
		values: zs,
		responsive: !0
	},
	top: {
		type: "enum | string",
		className: "rt-r-top",
		customProperties: ["--top"],
		values: zs,
		responsive: !0
	},
	right: {
		type: "enum | string",
		className: "rt-r-right",
		customProperties: ["--right"],
		values: zs,
		responsive: !0
	},
	bottom: {
		type: "enum | string",
		className: "rt-r-bottom",
		customProperties: ["--bottom"],
		values: zs,
		responsive: !0
	},
	left: {
		type: "enum | string",
		className: "rt-r-left",
		customProperties: ["--left"],
		values: zs,
		responsive: !0
	},
	overflow: {
		type: "enum",
		className: "rt-r-overflow",
		values: Ls,
		responsive: !0
	},
	overflowX: {
		type: "enum",
		className: "rt-r-ox",
		values: Ls,
		responsive: !0
	},
	overflowY: {
		type: "enum",
		className: "rt-r-oy",
		values: Ls,
		responsive: !0
	},
	flexBasis: {
		type: "string",
		className: "rt-r-fb",
		customProperties: ["--flex-basis"],
		responsive: !0
	},
	flexShrink: {
		type: "enum | string",
		className: "rt-r-fs",
		customProperties: ["--flex-shrink"],
		values: Bs,
		responsive: !0
	},
	flexGrow: {
		type: "enum | string",
		className: "rt-r-fg",
		customProperties: ["--flex-grow"],
		values: Vs,
		responsive: !0
	},
	gridArea: {
		type: "string",
		className: "rt-r-ga",
		customProperties: ["--grid-area"],
		responsive: !0
	},
	gridColumn: {
		type: "string",
		className: "rt-r-gc",
		customProperties: ["--grid-column"],
		responsive: !0
	},
	gridColumnStart: {
		type: "string",
		className: "rt-r-gcs",
		customProperties: ["--grid-column-start"],
		responsive: !0
	},
	gridColumnEnd: {
		type: "string",
		className: "rt-r-gce",
		customProperties: ["--grid-column-end"],
		responsive: !0
	},
	gridRow: {
		type: "string",
		className: "rt-r-gr",
		customProperties: ["--grid-row"],
		responsive: !0
	},
	gridRowStart: {
		type: "string",
		className: "rt-r-grs",
		customProperties: ["--grid-row-start"],
		responsive: !0
	},
	gridRowEnd: {
		type: "string",
		className: "rt-r-gre",
		customProperties: ["--grid-row-end"],
		responsive: !0
	},
	alignSelf: {
		type: "enum",
		className: "rt-r-as",
		values: Hs,
		responsive: !0
	},
	justifySelf: {
		type: "enum",
		className: "rt-r-js",
		values: Us,
		responsive: !0
	}
}, Gs = [
	"1",
	"2",
	"3",
	"4"
], Ks = [
	"classic",
	"solid",
	"soft",
	"surface",
	"outline",
	"ghost"
], qs = {
	...R,
	size: {
		type: "enum",
		className: "rt-r-size",
		values: Gs,
		default: "2",
		responsive: !0
	},
	variant: {
		type: "enum",
		className: "rt-variant",
		values: Ks,
		default: "solid"
	},
	...$o,
	...es,
	...Cs,
	loading: {
		type: "boolean",
		className: "rt-loading",
		default: !1
	}
}, Js = [
	"0",
	"1",
	"2",
	"3",
	"4",
	"5",
	"6",
	"7",
	"8",
	"9"
], Ys = {
	gap: {
		type: "enum | string",
		className: "rt-r-gap",
		customProperties: ["--gap"],
		values: Js,
		responsive: !0
	},
	gapX: {
		type: "enum | string",
		className: "rt-r-cg",
		customProperties: ["--column-gap"],
		values: Js,
		responsive: !0
	},
	gapY: {
		type: "enum | string",
		className: "rt-r-rg",
		customProperties: ["--row-gap"],
		values: Js,
		responsive: !0
	}
}, Xs = ["div", "span"], Zs = [
	"none",
	"inline-flex",
	"flex"
], Qs = [
	"row",
	"column",
	"row-reverse",
	"column-reverse"
], $s = [
	"start",
	"center",
	"end",
	"baseline",
	"stretch"
], ec = [
	"start",
	"center",
	"end",
	"between"
], tc = [
	"nowrap",
	"wrap",
	"wrap-reverse"
], nc = {
	as: {
		type: "enum",
		values: Xs,
		default: "div"
	},
	...R,
	display: {
		type: "enum",
		className: "rt-r-display",
		values: Zs,
		responsive: !0
	},
	direction: {
		type: "enum",
		className: "rt-r-fd",
		values: Qs,
		responsive: !0
	},
	align: {
		type: "enum",
		className: "rt-r-ai",
		values: $s,
		responsive: !0
	},
	justify: {
		type: "enum",
		className: "rt-r-jc",
		values: ec,
		parseValue: rc,
		responsive: !0
	},
	wrap: {
		type: "enum",
		className: "rt-r-fw",
		values: tc,
		responsive: !0
	},
	...Ys
};
function rc(e) {
	return e === "between" ? "space-between" : e;
}
// node_modules/@radix-ui/themes/dist/esm/components/flex.js
var ic = _.forwardRef((e, t) => {
	let { className: n, asChild: r, as: i = "div", ...a } = ms(e, nc, Ws, gs);
	return _.createElement(r ? Ps : i, {
		...a,
		ref: t,
		className: (0, fs.default)("rt-Flex", n)
	});
});
ic.displayName = "Flex";
// node_modules/@radix-ui/themes/dist/esm/components/spinner.props.js
var ac = {
	size: {
		type: "enum",
		className: "rt-r-size",
		values: [
			"1",
			"2",
			"3"
		],
		default: "2",
		responsive: !0
	},
	loading: {
		type: "boolean",
		default: !0
	}
}, oc = _.forwardRef((e, t) => {
	let { className: n, children: r, loading: i, ...a } = ms(e, ac, gs);
	if (!i) return r;
	let o = _.createElement("span", {
		...a,
		ref: t,
		className: (0, fs.default)("rt-Spinner", n)
	}, _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }), _.createElement("span", { className: "rt-SpinnerLeaf" }));
	return r === void 0 ? o : _.createElement(ic, {
		asChild: !0,
		position: "relative",
		align: "center",
		justify: "center"
	}, _.createElement("span", null, _.createElement("span", {
		"aria-hidden": !0,
		style: {
			display: "contents",
			visibility: "hidden"
		},
		inert: void 0
	}, r), _.createElement(ic, {
		asChild: !0,
		align: "center",
		justify: "center",
		position: "absolute",
		inset: "0"
	}, _.createElement("span", null, o))));
});
oc.displayName = "Spinner";
// node_modules/@radix-ui/themes/dist/esm/components/visually-hidden.js
var sc = A;
// node_modules/@radix-ui/themes/dist/esm/helpers/map-prop-values.js
function cc(e, t) {
	if (e !== void 0) return typeof e == "string" ? t(e) : Object.fromEntries(Object.entries(e).map(([e, n]) => [e, t(n)]));
}
function lc(e) {
	switch (e) {
		case "1": return "1";
		case "2":
		case "3": return "2";
		case "4": return "3";
	}
}
// node_modules/@radix-ui/themes/dist/esm/components/_internal/base-button.js
var uc = _.forwardRef((e, t) => {
	let { size: n = qs.size.default } = e, { className: r, children: i, asChild: a, color: o, radius: s, disabled: c = e.loading, ...l } = ms(e, qs, gs), u = a ? ee : "button";
	return _.createElement(u, {
		"data-disabled": c || void 0,
		"data-accent-color": o,
		"data-radius": s,
		...l,
		ref: t,
		className: (0, fs.default)("rt-reset", "rt-BaseButton", r),
		disabled: c
	}, e.loading ? _.createElement(_.Fragment, null, _.createElement("span", {
		style: {
			display: "contents",
			visibility: "hidden"
		},
		"aria-hidden": !0
	}, i), _.createElement(sc, null, i), _.createElement(ic, {
		asChild: !0,
		align: "center",
		justify: "center",
		position: "absolute",
		inset: "0"
	}, _.createElement("span", null, _.createElement(oc, { size: cc(n, lc) })))) : i);
});
uc.displayName = "BaseButton";
// node_modules/@radix-ui/themes/dist/esm/components/button.js
var dc = _.forwardRef(({ className: e, ...t }, n) => _.createElement(uc, {
	...t,
	ref: n,
	className: (0, fs.default)("rt-Button", e)
}));
dc.displayName = "Button";
// node_modules/@radix-ui/themes/dist/esm/components/icon-button.js
var fc = _.forwardRef(({ className: e, ...t }, n) => _.createElement(uc, {
	...t,
	ref: n,
	className: (0, fs.default)("rt-IconButton", e)
}));
fc.displayName = "IconButton";
// node_modules/@radix-ui/themes/dist/esm/components/popover.props.js
var pc = [
	"1",
	"2",
	"3",
	"4"
], mc = {
	...R,
	size: {
		type: "enum",
		className: "rt-r-size",
		values: pc,
		default: "2",
		responsive: !0
	},
	width: Jo.width,
	minWidth: Jo.minWidth,
	maxWidth: {
		...Jo.maxWidth,
		default: "480px"
	},
	...Yo
}, hc = (e) => _.createElement(lo, { ...e });
hc.displayName = "Popover.Root";
var gc = _.forwardRef(({ children: e, ...t }, n) => _.createElement(mo, {
	...t,
	ref: n,
	asChild: !0
}, Ns(e)));
gc.displayName = "Popover.Trigger";
var _c = _.forwardRef((e, t) => {
	let { className: n, forceMount: r, container: i, ...a } = ms(e, mc);
	return _.createElement(vo, {
		container: i,
		forceMount: r
	}, _.createElement(As, { asChild: !0 }, _.createElement(bo, {
		align: "start",
		sideOffset: 8,
		collisionPadding: 10,
		...a,
		ref: t,
		className: (0, fs.default)("rt-PopperContent", "rt-PopoverContent", n)
	})));
});
_c.displayName = "Popover.Content";
var vc = _.forwardRef(({ children: e, ...t }, n) => _.createElement(Eo, {
	...t,
	ref: n,
	asChild: !0
}, Ns(e)));
vc.displayName = "Popover.Close";
var yc = _.forwardRef(({ children: e, ...t }, n) => _.createElement(fo, {
	...t,
	ref: n
}));
yc.displayName = "Popover.Anchor";
// node_modules/@radix-ui/themes/dist/esm/components/text-field.props.js
var bc = {
	size: {
		type: "enum",
		className: "rt-r-size",
		values: [
			"1",
			"2",
			"3"
		],
		default: "2",
		responsive: !0
	},
	variant: {
		type: "enum",
		className: "rt-variant",
		values: [
			"classic",
			"surface",
			"soft"
		],
		default: "surface"
	},
	...Qo,
	...Cs
}, xc = {
	side: {
		type: "enum",
		values: ["left", "right"]
	},
	...Qo,
	gap: nc.gap,
	px: Is.px,
	pl: Is.pl,
	pr: Is.pr
}, Sc = _.forwardRef((e, t) => {
	let n = _.useRef(null), { children: r, className: i, color: a, radius: o, style: s, ...c } = ms(e, bc, gs);
	return _.createElement("div", {
		"data-accent-color": a,
		"data-radius": o,
		style: s,
		className: (0, fs.default)("rt-TextFieldRoot", i),
		onPointerDown: (e) => {
			let t = e.target;
			if (t.closest("input, button, a")) return;
			let r = n.current;
			if (!r) return;
			let i = t.closest("\n            .rt-TextFieldSlot[data-side='right'],\n            .rt-TextFieldSlot:not([data-side='right']) ~ .rt-TextFieldSlot:not([data-side='left'])\n          ") ? r.value.length : 0;
			requestAnimationFrame(() => {
				try {
					r.setSelectionRange(i, i);
				} catch {}
				r.focus();
			});
		}
	}, _.createElement("input", {
		spellCheck: "false",
		...c,
		ref: x(n, t),
		className: "rt-reset rt-TextFieldInput"
	}), r);
});
Sc.displayName = "TextField.Root";
var Cc = _.forwardRef((e, t) => {
	let { className: n, color: r, side: i, ...a } = ms(e, xc);
	return _.createElement("div", {
		"data-accent-color": r,
		"data-side": i,
		...a,
		ref: t,
		className: (0, fs.default)("rt-TextFieldSlot", n)
	});
});
Cc.displayName = "TextField.Slot";
// node_modules/@radix-ui/themes/tokens/base.css
var wc = g();
// frontend/session/reader.ts
function Tc(e, t, n, r) {
	return n === "failed" || n === "waiting" || /(?:error|failed|blocked|ask_user|question|approval|retry_scheduled|retrying)/.test(e) ? "attention" : t === "summary" || t === "outcome" || /(?:report\.ready|file\.shared|master\.done)/.test(e) || /(?:vuln\.confirmed|flag\.(?:accepted|submitted))/.test(e) ? "result" : /\btl-(?:user|conv)\b/.test(r) ? "message" : t === "continuation" ? "progress" : "activity";
}
function Ec(e, t) {
	let n = t.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean), r = `${e.title}\n${e.actor}\n${e.text}`.toLocaleLowerCase();
	return n.every((e) => r.includes(e));
}
function Dc(e, t, n) {
	let r = (e || t).replace(/\s+/g, " ").trim();
	return r ? [...r].slice(0, 88).join("") + ([...r].length > 88 ? "…" : "") : n;
}
function Oc(e) {
	return [
		"message",
		"result",
		"attention",
		"progress",
		"activity"
	].map((t) => ({
		kind: t,
		entries: e.filter((e) => e.kind === t)
	})).filter((e) => e.entries.length);
}
// frontend/session/bridge.ts
function kc() {
	let e = document.querySelector("#view-session"), t = document.querySelector("#trace-box"), n = globalThis.window?.TraceViewport, r = () => n?.rows() || [...t.querySelectorAll(":scope > .tl")], i = matchMedia("(prefers-color-scheme: dark)"), a = /* @__PURE__ */ new Set(), o = /* @__PURE__ */ new Map(), s = /* @__PURE__ */ new WeakMap(), c = /* @__PURE__ */ new WeakMap(), l = 0, u = {
		sessionId: "",
		entries: [],
		appearance: "light",
		language: "zh-CN"
	}, d, f = "", p = "", m, h = () => {
		let e = document.documentElement.dataset.theme;
		return e === "dark" || e !== "light" && i.matches ? "dark" : "light";
	};
	function g() {
		let t = e.dataset.sessionId || "";
		return t !== p && (f = "", p = t, m?.classList.remove("reader-target")), t;
	}
	function _(e) {
		let t = c.get(e);
		if (t) return t;
		let n = s.get(e);
		n || (n = `reader-entry-${++l}`, s.set(e, n));
		let r = e.querySelector(".task-heading strong"), i = e.querySelector(".btxt, .task-stage-summary, .task-summary, .tl-say, .ang, .m"), a = i?.querySelector("h1, h2, h3"), o = e.querySelector(".task-seal")?.getAttribute("aria-label") || e.querySelector(".who")?.textContent?.trim() || e.dataset.agent || "", u = e.querySelector(".task-context")?.textContent?.trim() || "", d = e.dataset.eventType || "", f = e.textContent?.trim() || "", p = Tc(d, e.dataset.taskPart || "", e.dataset.taskStatus || "", e.className), m = document.documentElement.lang.startsWith("en"), h = m ? {
			message: "Conversation",
			result: "Result",
			attention: "Needs attention",
			progress: "Task progress",
			activity: "Activity"
		}[p] : {
			message: "会话消息",
			result: "结果与交付",
			attention: "待关注事项",
			progress: "任务进展",
			activity: "运行记录"
		}[p], g = e.querySelector(".fc-name")?.textContent?.trim() || "";
		return t = {
			id: n,
			title: d === "report.ready" ? m ? "Report ready" : "解题报告已生成" : Dc(a?.textContent?.trim() || r?.textContent?.trim() || (i ? "" : e.querySelector(".hd .k")?.textContent?.trim()) || "", i?.textContent?.trim() || g, h),
			text: f,
			kind: p,
			actor: u || o,
			time: e.querySelector("time")?.textContent || ""
		}, c.set(e, t), t;
	}
	function v() {
		d = void 0;
		let e = g();
		o.clear();
		let t = [];
		for (let e of r()) {
			if (e.hidden) continue;
			let n = _(e);
			o.set(n.id, e), t.push(n);
		}
		u = {
			sessionId: e,
			entries: t,
			appearance: h(),
			language: document.documentElement.lang
		}, a.forEach((e) => e());
	}
	function y() {
		d ??= setTimeout(v, 120);
	}
	function b(e = t) {
		g();
		for (let n of e === t ? r() : e.querySelectorAll(":scope > .tl")) n.classList.toggle("reader-filtered", !!f && !Ec(_(n), f));
	}
	let x = (e) => b(e.detail);
	t.addEventListener("reader:render", x);
	let S = n?.subscribe((e) => {
		for (let t of e) c.delete(t);
		b(), y();
	}), C = new MutationObserver((e) => {
		for (let t of e) {
			let e = (t.target instanceof Element ? t.target : t.target.parentElement)?.closest(".tl");
			e && c.delete(e);
		}
		b(), y();
	});
	return n || C.observe(t, {
		childList: !0,
		characterData: !0,
		subtree: !0,
		attributes: !0,
		attributeFilter: [
			"hidden",
			"data-task-status",
			"data-task-part",
			"data-event-type"
		]
	}), C.observe(e, {
		attributes: !0,
		attributeFilter: ["data-session-id"]
	}), C.observe(document.documentElement, {
		attributes: !0,
		attributeFilter: ["data-theme", "lang"]
	}), i.addEventListener("change", y), b(), v(), {
		subscribe(e) {
			return a.add(e), () => {
				a.delete(e);
			};
		},
		getSnapshot: () => u,
		filter(e) {
			f = e, b(), n?.refresh();
		},
		navigate(r) {
			let i = o.get(r);
			i && (i.isConnected || n?.has(i)) && u.sessionId === e.dataset.sessionId && (t.dispatchEvent(new Event("reader:navigate")), m?.classList.remove("reader-target"), i.classList.add("reader-target"), m = i, n ? n.navigate(i, "center") : (i.scrollIntoView({
				block: "center",
				behavior: "instant"
			}), i.tabIndex = -1, i.focus({ preventScroll: !0 })));
		},
		destroy() {
			t.removeEventListener("reader:render", x), S?.(), C.disconnect(), i.removeEventListener("change", y), d !== void 0 && clearTimeout(d);
			for (let e of r()) e.classList.remove("reader-filtered", "reader-target");
			a.clear();
		}
	};
}
// node_modules/@tanstack/virtual-core/dist/esm/lazy-measurements.js
function Ac(e) {
	return typeof e == "object" ? e.key : e;
}
function jc(e, t) {
	let n = e.length;
	return new Proxy(e, { get(e, r, i) {
		if (typeof r == "string") {
			let i = r.charCodeAt(0);
			if (i >= 48 && i <= 57) {
				let i = +r;
				if (Number.isInteger(i) && i >= 0 && i < n) {
					let n = e[i];
					if (typeof n != "object") {
						let r = t[i * 2];
						n = e[i] = {
							index: i,
							key: n,
							start: r,
							size: t[i * 2 + 1],
							end: r + t[i * 2 + 1],
							lane: 0
						};
					}
					return n;
				}
			}
			if (r === "length") return n;
		}
		return Reflect.get(e, r, i);
	} });
}
// node_modules/@tanstack/virtual-core/dist/esm/utils.js
function Mc(e, t, n) {
	let r = n.initialDeps ?? [], i, a = !0;
	function o() {
		let o = e();
		return (o.length !== r.length || o.some((e, t) => r[t] !== e)) && (r = o, i = t(...o), n?.onChange && !(a && n.skipInitialOnChange) && n.onChange(i), a = !1), i;
	}
	return o.updateDeps = (e) => {
		r = e;
	}, o;
}
function Nc(e, t) {
	if (e === void 0) throw Error(`Unexpected undefined${t ? `: ${t}` : ""}`);
	return e;
}
var Pc = (e, t) => Math.abs(e - t) < 1.01, Fc = (e, t, n) => {
	let r;
	return Object.assign(function(...i) {
		e.clearTimeout(r), r = e.setTimeout(() => t.apply(this, i), n);
	}, { cancel: () => {
		e.clearTimeout(r);
	} });
};
// node_modules/@tanstack/virtual-core/dist/esm/scroll-classifier.js
function Ic({ offset: e, isScrolling: t, elementMax: n, holdOffset: r, scrollOffset: i, pendingWrite: a, clampedMaxAtWrite: o }) {
	return t && a === null && e === i ? "spurious" : a !== null && Math.abs(e - a.target) < 1.5 ? "readBack" : a !== null && !a.smooth && n !== null && a.target > n + r + .5 && Math.abs(e - (n + r)) < 1.5 || a !== null && !a.smooth && a.lowMaxes?.some((t) => Math.abs(e - t) < 1.5) ? "clampedReadBack" : o !== null && Math.abs(e - o) < 1.5 ? "clampedEcho" : "external";
}
var Lc = (e) => e === "smooth" || e === "smoothFinishing";
function Rc({ kind: e, offset: t, command: n, target: r }) {
	return e !== "spurious" && e !== "readBack" && e !== "clampedReadBack" && n?.index != null && n.phase !== "queued" && !Lc(n.phase) && !Pc(t, n.lastTargetOffset) && !Pc(t, r()) && e !== "clampedEcho";
}
var zc = 40;
function Bc(e) {
	let t = e[e.length - 1];
	if (t === void 0) return !1;
	let n = e.length - 2;
	for (; n >= 0 && t.t - e[n].t < zc;) n--;
	if (n < 0) return !1;
	let r = e[n], i = n - 1;
	for (; i >= 0 && r.t - e[i].t < zc;) i--;
	if (i < 0) return !1;
	let a = e[i], o = (t.offset - r.offset) / (t.t - r.t), s = (r.offset - a.offset) / (r.t - a.t);
	return o * s >= 0 && Math.abs(o) >= 2 * Math.abs(s) && Math.abs(o) - Math.abs(s) >= .8;
}
function Vc(e) {
	let t = e[e.length - 1];
	if (t === void 0) return !1;
	let n = e.length - 2;
	for (; n >= 0 && t.t - e[n].t < zc;) n--;
	if (n < 0) return !1;
	let r = e[n];
	return Math.abs(t.offset - r.offset) / (t.t - r.t) >= .05;
}
// node_modules/@tanstack/virtual-core/dist/esm/ios-gesture.js
var Hc, Uc = () => {
	if (Hc !== void 0) return Hc;
	if (typeof navigator > "u") return Hc = !1;
	if (/iP(hone|od|ad)/.test(navigator.userAgent)) return Hc = !0;
	let e = navigator.maxTouchPoints;
	return Hc = navigator.platform === "MacIntel" && e !== void 0 && e > 0;
}, Wc, Gc = () => Wc === void 0 ? Wc = Uc() && typeof CSS < "u" && typeof CSS.supports == "function" && CSS.supports("overflow-anchor: auto") : Wc, Kc = () => !Uc() || Gc(), qc = {
	capture: !0,
	passive: !0
}, Jc = class {
	constructor(e) {
		this.host = e, this._touch = null, this.inMotion = () => this._touch !== null && this._touch.phase !== "lateFling", this.dragging = () => this._touch?.phase === "down" && this._touch.moved, this.tail = () => this._touch?.phase === "tail" ? this._touch.moved ? "fling" : "tap" : null, this.onScroll = ({ isScrolling: e, ownReadBack: t, moved: n, external: r }) => (e && this._touch?.phase === "tail" ? this._openTail(this._touch.moved || !t, !1) : this._touch?.phase === "lateFling" && (this.host.now() > this._touch.until ? this._touch = null : r && e && n && this._openTail(!0, !1)), !t && e && n && this._touch?.phase === "down" ? (this._touch.moved = !0, !0) : !1), this.touchStart = () => {
			this.end(), this._touch = {
				phase: "down",
				moved: !1
			};
		}, this.touchEnd = () => {
			let e = this._touch?.phase === "down" && this._touch.moved;
			this.end(), this._openTail(e, e);
		}, this.end = () => {
			let e = this.host.getWindow();
			this._touch?.phase === "tail" && e != null && e.clearTimeout(this._touch.timerId), this._touch = null;
		}, this.listen = (e) => {
			let t = () => this.touchStart(), n = () => this.touchEnd();
			return e.addEventListener("touchstart", t, qc), e.addEventListener("touchend", n, qc), e.addEventListener("touchcancel", n, qc), () => {
				e.removeEventListener("touchstart", t, qc), e.removeEventListener("touchend", n, qc), e.removeEventListener("touchcancel", n, qc), this.end();
			};
		}, this._openTail = (e, t) => {
			let n = this.host.getWindow();
			if (n == null) return;
			this.end();
			let r = {
				phase: "tail",
				moved: e,
				awaitingFling: t,
				timerId: n.setTimeout(() => {
					this.host.tailStillMoving() ? this._openTail(r.moved, r.awaitingFling) : (this._touch = r.awaitingFling ? {
						phase: "lateFling",
						until: this.host.now() + 1e3
					} : null, this.host.onTailClosed());
				}, 150)
			};
			this._touch = r;
		};
	}
	get touch() {
		return this._touch;
	}
}, Yc = (e) => {
	let { clientWidth: t, clientHeight: n } = e;
	return {
		width: t,
		height: n
	};
}, Xc = (e) => {
	let t = /* @__PURE__ */ new Set();
	return {
		schedule: (n) => {
			let r = e.requestAnimationFrame(() => {
				t.delete(r), n();
			});
			t.add(r);
		},
		cancel: () => {
			t.forEach((t) => e.cancelAnimationFrame(t)), t.clear();
		}
	};
}, Zc = (e) => e, Qc = (e) => {
	let t = e.overscan * Math.max(e.lanes ?? 1, 1), n = Math.max(e.startIndex - t, 0), r = Math.min(e.endIndex + t, e.count - 1) - n + 1, i = Array(r);
	for (let e = 0; e < r; e++) i[e] = n + e;
	return i;
}, $c = (e, t) => {
	let n = e.scrollElement;
	if (!n) return;
	let r = e.targetWindow;
	if (!r) return;
	let i = (e) => {
		let { width: n, height: r } = e;
		t({
			width: Math.round(n),
			height: Math.round(r)
		});
	};
	if (i(Yc(n)), !r.ResizeObserver) return () => {};
	let a = Xc(r), o = new r.ResizeObserver(() => {
		let t = () => {
			i(Yc(n));
		};
		e.options.useAnimationFrameWithResizeObserver ? a.schedule(t) : t();
	});
	return o.observe(n, { box: "content-box" }), () => {
		o.unobserve(n), a.cancel();
	};
}, el = { passive: !0 }, tl = typeof window > "u" || "onscrollend" in window, nl = (e, t, n) => {
	let r = e.scrollElement;
	if (!r) return;
	let i = e.targetWindow;
	if (!i) return;
	let a = e.options.useScrollendEvent && tl, o = 0, s = a ? null : Fc(i, () => t(n(r), !1), e.options.isScrollingResetDelay), c = (e) => () => {
		o = n(r), s?.(), t(o, e);
	}, l = c(!0), u = c(!1);
	return r.addEventListener("scroll", l, el), a && r.addEventListener("scrollend", u, el), () => {
		r.removeEventListener("scroll", l), a && r.removeEventListener("scrollend", u), s?.cancel();
	};
}, rl = (e, t) => nl(e, t, (t) => {
	let { horizontal: n, isRtl: r } = e.options;
	return n ? t.scrollLeft * (r && -1 || 1) : t.scrollTop;
}), il = (e, t, n) => {
	if (n.options.useCachedMeasurements) {
		let t = n.indexFromElement(e), r = n.options.getItemKey(t);
		return n.itemSizeCache.get(r) ?? n.options.estimateSize(t);
	}
	if (t?.borderBoxSize) {
		let e = t.borderBoxSize[0];
		if (e) return Math.round(e[n.options.horizontal ? "inlineSize" : "blockSize"]);
	}
	if (!t) {
		let t = n.indexFromElement(e), r = n.options.getItemKey(t), i = n.itemSizeCache.get(r);
		if (i !== void 0) return i;
	}
	return e[n.options.horizontal ? "offsetWidth" : "offsetHeight"];
}, al = (e, { adjustments: t = 0, behavior: n }, r) => {
	var i;
	let { scrollElement: a, options: o } = r, s = e + t, c = o.horizontal && o.isRtl && a !== null && "ownerDocument" in a;
	(i = a?.scrollTo) == null || i.call(a, {
		[o.horizontal ? "left" : "top"]: c ? -s : s,
		behavior: n
	});
}, ol = (e, t) => e === "smooth" ? t ? "smoothLanded" : "smooth" : "instant", sl = (e) => e?.phase === "smooth" || e?.phase === "smoothLanded";
function cl(e, t, n, r) {
	if (t === 0) return !1;
	let i = r(0), a = /* @__PURE__ */ new Set(), o = 0;
	for (; o < e;) {
		let e = n(o);
		if (e === i) break;
		a.add(e), o++;
	}
	let s = e - o;
	if (s === 0 || s >= t) return !1;
	for (let e = 0; e < s; e++) if (r(e) !== n(o + e)) return !1;
	for (let e = s; e < t; e++) if (a.has(r(e))) return !1;
	return !0;
}
var ll = class {
	constructor(e) {
		this.unsubs = [], this.scrollElement = null, this.targetWindow = null, this.isScrolling = !1, this.scrollState = null, this.measurementsCache = [], this._singleLaneMeasurements = null, this.itemSizeCache = /* @__PURE__ */ new Map(), this.itemSizeCacheVersion = 0, this.laneAssignments = /* @__PURE__ */ new Map(), this.pendingMin = null, this.prevLanes = void 0, this.lanesChangedFlag = !1, this.lanesSettling = !1, this.pendingScrollAnchor = null, this.scrollRect = null, this.scrollOffset = null, this.scrollDirection = null, this._holdOffset = 0, this._reportedOverscroll = !1, this._iosGesture = new Jc({
			getWindow: () => this.targetWindow,
			now: () => this.now(),
			tailStillMoving: () => {
				let e = this.scrollState;
				if (e?.phase !== "queued" || this.pendingWrite !== null) return !1;
				let t = this._readElementOffset();
				return t === null || this._lastElementOffset === null || Math.abs(t - this._lastElementOffset) < 1 ? !1 : (this._lastElementOffset = t, e.samples = [...e.samples ?? [], {
					t: this.now(),
					offset: t
				}], !0);
			},
			onTailClosed: () => {
				let e = this.scrollState;
				e?.phase === "queued" && Vc(e.samples ?? []) && (this.scrollState = null), this._flushHoldIfReady(!0), this._writeAwaitingCommand(), this._retryClampedAdjustment();
			}
		}), this._lastElementOffset = null, this.pendingWrite = null, this._clampedAdjustment = null, this.elementsCache = /* @__PURE__ */ new Map(), this._resizeBatch = null, this.now = () => {
			var e;
			return ((e = this.targetWindow?.performance)?.now)?.call(e) ?? Date.now();
		}, this.observer = /* @__PURE__ */ (() => {
			let e = null, t = null, n = !1, r = !1, i = /* @__PURE__ */ new Set(), a = () => {
				if (e) return e;
				let r = this.targetWindow;
				if (!r || !r.ResizeObserver) return null;
				let i = t = Xc(r);
				return e = new r.ResizeObserver((e) => {
					let t = () => {
						let t = { sync: !1 };
						this._resizeBatch = t;
						try {
							e.forEach(o);
						} finally {
							this._resizeBatch = null, t.notify && (this.notify(t.sync), this._noteClampMax(), this._flushHoldIfReady(!0), this._retryClampedAdjustment(), this.measureElement(null));
						}
					};
					if (this.options.useAnimationFrameWithResizeObserver) i.schedule(t);
					else {
						n = !0;
						try {
							t();
						} finally {
							n = !1;
						}
					}
				});
			}, o = (e) => {
				let t = e.target, n = this.indexFromElement(t);
				if (t.isConnected) this.isIndexInRange(n) && this.shouldMeasureDuringScroll(n) && this.resizeItem(n, this.options.measureElement(t, e, this));
				else {
					this.observer.unobserve(t);
					for (let [e, n] of this.elementsCache) if (n === t) {
						this.elementsCache.delete(e);
						break;
					}
				}
			};
			return {
				disconnect: () => {
					var n;
					(n = a()) == null || n.disconnect(), t?.cancel(), i.clear(), r = !1, e = null, t = null;
				},
				observe: (o) => {
					let s = a();
					s && (n ? (i.add(o), r || (r = !0, t?.schedule(() => {
						r = !1, i.forEach((t) => {
							t.isConnected && e?.observe(t, { box: "border-box" });
						}), i.clear();
					}))) : s.observe(o, { box: "border-box" }));
				},
				unobserve: (e) => {
					var t;
					i.delete(e), (t = a()) == null || t.unobserve(e);
				}
			};
		})(), this.range = null, this.setOptions = (e) => {
			let t = {
				debug: !1,
				initialOffset: 0,
				overscan: 1,
				paddingStart: 0,
				paddingEnd: 0,
				scrollPaddingStart: 0,
				scrollPaddingEnd: 0,
				horizontal: !1,
				getItemKey: Zc,
				rangeExtractor: Qc,
				onChange: () => {},
				measureElement: il,
				initialRect: {
					width: 0,
					height: 0
				},
				scrollMargin: 0,
				gap: 0,
				indexAttribute: "data-index",
				initialMeasurementsCache: [],
				lanes: 1,
				anchorTo: "start",
				followOnAppend: !1,
				scrollEndThreshold: 1,
				isScrollingResetDelay: 150,
				enabled: !0,
				isRtl: !1,
				useScrollendEvent: !1,
				useAnimationFrameWithResizeObserver: !1,
				laneAssignmentMode: "estimate",
				useCachedMeasurements: !1
			};
			for (let n in e) {
				let r = e[n];
				r !== void 0 && (t[n] = r);
			}
			let n = this.options, r = null, i = null, a = !1;
			if (n !== void 0 && n.enabled && t.enabled && t.anchorTo === "end" && this.scrollElement !== null) {
				let e = n.count, o = t.count, s = this.getMeasurements(), c = this._singleLaneMeasurements?.items ?? s, l = (e) => Ac(c[e]), u = e > 0 ? l(0) : null, d = e > 0 ? l(e - 1) : null;
				if (o !== e || e > 0 && o > 0 && (t.getItemKey(0) !== u || t.getItemKey(o - 1) !== d)) {
					a = !0;
					let c = e > 0 ? this.getVirtualItemForOffset(this.getScrollOffset()) ?? s[0] : null;
					c && (r = [c.key, this.getScrollOffset() - c.start]);
					let u = t.followOnAppend === !0 ? "auto" : t.followOnAppend || null;
					u && o > 0 && this.isAtEnd(n.scrollEndThreshold) && (e === 0 || t.getItemKey(o - 1) !== d) && (o > e || cl(e, o, l, t.getItemKey)) && (i = u);
				}
			}
			this.options = t, a && (this.pendingMin = 0, this.itemSizeCacheVersion++);
			let o = null;
			if (r && this.scrollOffset !== null && !this._awaitingCommandReadBack()) {
				let [e, t] = r, n = this.getMeasurements(), { count: a, getItemKey: s } = this.options, c = 0;
				for (; c < a && s(c) !== e;) c++;
				if (c < a) {
					let e = n[c];
					if (e) {
						let n = Math.max(0, e.start + t);
						!i && n !== this.scrollOffset && (o = n - this.scrollOffset, this.scrollOffset = n);
					}
				}
			}
			i ? this.pendingScrollAnchor = {
				kind: "follow",
				behavior: i
			} : o !== null && (this._canWriteScroll() ? this.pendingScrollAnchor = {
				kind: "anchor",
				delta: o
			} : (this._holdOffset += o, this._followOffsetTarget(o)));
		}, this.notify = (e) => {
			var t, n;
			(n = (t = this.options).onChange) == null || n.call(t, this, e);
		}, this.maybeNotify = Mc(() => (this.calculateRange(), [
			this.isScrolling,
			this.range ? this.range.startIndex : null,
			this.range ? this.range.endIndex : null
		]), (e) => {
			this.notify(e);
		}, {
			key: !1,
			debug: () => this.options.debug,
			initialDeps: [
				this.isScrolling,
				this.range ? this.range.startIndex : null,
				this.range ? this.range.endIndex : null
			]
		}), this.cleanup = () => {
			var e;
			this.unsubs.filter(Boolean).forEach((e) => e()), this.unsubs = [], this.observer.disconnect(), this.rafId != null && this.targetWindow && (this.targetWindow.cancelAnimationFrame(this.rafId), this.rafId = null), this.scrollState = null, (e = this._momentumStop) == null || e.restore(), this._momentumStop = null, this.isScrolling = !1, this.scrollDirection = null, this._holdOffset = 0, this._reportedOverscroll = !1, this._clampedAdjustment = null, this.pendingWrite = null, this.scrollElement = null, this.targetWindow = null;
		}, this._didMount = () => () => {
			this.cleanup();
		}, this._willUpdate = () => {
			let e = this.options.enabled ? this.options.getScrollElement() : null;
			if (this.scrollElement !== e) {
				if (this.cleanup(), !e) {
					this.maybeNotify();
					return;
				}
				this.scrollElement = e, this.targetWindow = this.scrollElement && "ownerDocument" in this.scrollElement ? this.scrollElement.ownerDocument.defaultView : this.scrollElement?.window ?? null, this.elementsCache.forEach((e) => {
					this.observer.observe(e);
				}), this.unsubs.push(this.options.observeElementRect(this, (e) => {
					this.scrollRect = e, this.maybeNotify();
				})), this.unsubs.push(this.options.observeElementOffset(this, (e, t) => {
					let n = t ? this._elementMaxScrollOffset() : null, r = this._reportedOverscroll;
					this._reportedOverscroll = e < 0 || n !== null && e > n + 1;
					let i = e;
					this._lastElementOffset = i, e = this._toLayoutOffset(e);
					let a = this._classifyScrollEvent(e, t, n);
					if (a === "spurious") return;
					a === "readBack" ? e = this.pendingWrite.target : a === "clampedReadBack" && this.scrollState?.index != null && e < this.getMaxScrollOffset() - 1.5 ? this.scrollState.lastTargetOffset = e : Rc({
						kind: a,
						offset: e,
						command: this.scrollState,
						target: () => this.getScrollStateTarget(this.scrollState)
					}) && (this.scrollState = null), this.pendingWrite = null, this._clampedAdjustment !== null && Math.abs(e - this._clampedAdjustment.maxAtWrite) >= 1.5 && (this._clampedAdjustment = null);
					let o = this.getScrollOffset(), s = this.scrollDirection;
					this.scrollDirection = t ? o === e ? this.scrollDirection : o < e ? "forward" : "backward" : null, this.scrollOffset = e, this.isScrolling = t;
					let c = this.scrollState;
					if (c?.phase === "queued" && a === "external" && t) {
						if (r || this._reportedOverscroll) c.samples = [];
						else {
							let e = this.now();
							c.samples = [...(c.samples ?? []).filter((t) => e - t.t <= 200), {
								t: e,
								offset: i
							}], (s !== null && this.scrollDirection !== s || Bc(c.samples)) && (this.scrollState = null);
						}
					}
					this._iosGesture.onScroll({
						isScrolling: t,
						ownReadBack: a === "readBack",
						moved: e !== o,
						external: a === "external" && this.scrollState?.phase !== "smooth" && this.scrollState?.phase !== "smoothFinishing"
					}) && this.scrollState?.phase === "queued" && (this.scrollState = null), this._flushHoldIfReady(!0), this.scrollState && this.scheduleScrollReconcile(), this.maybeNotify();
				})), "addEventListener" in this.scrollElement && this.unsubs.push(this._iosGesture.listen(this.scrollElement)), this._scrollToOffset(this.getScrollOffset());
			}
			this._noteClampMax();
			let t = this.pendingScrollAnchor;
			this.pendingScrollAnchor = null, t && this.scrollElement && this.options.enabled && (t.kind === "anchor" ? this._canWriteScroll() ? sl(this.scrollState) && !Pc(this.getScrollOffset() - t.delta, this.scrollState.lastTargetOffset) || (this._followOffsetTarget(t.delta), this._scrollToOffset(this.getScrollOffset())) : (this._holdOffset += t.delta, this._followOffsetTarget(t.delta), this.notify(!1)) : this.scrollToEnd({ behavior: t.behavior })), this._flushHoldIfReady(!1), this._retryClampedAdjustment();
		}, this._classifyScrollEvent = (e, t, n) => Ic({
			offset: e,
			isScrolling: t,
			elementMax: n,
			holdOffset: this._holdOffset,
			scrollOffset: this.scrollOffset,
			pendingWrite: this.pendingWrite,
			clampedMaxAtWrite: this._clampedAdjustment?.maxAtWrite ?? null
		}), this._retryClampedAdjustment = () => {
			if (this._clampedAdjustment === null || !this.scrollElement || !this.options.enabled || !this._canWriteScroll()) return;
			let { target: e, maxAtWrite: t } = this._clampedAdjustment, n = this.getMaxScrollOffset();
			n > t + .5 && (this._clampedAdjustment = e > n + .5 ? {
				target: e,
				maxAtWrite: n
			} : null, this._scrollToOffset(e));
		}, this._writeAwaitingCommand = () => {
			let e = this.scrollState;
			if (e === null || e.phase !== "queued") return;
			let { behavior: t } = e;
			this.getMeasurements();
			let n = this.getScrollStateTarget(e);
			e.lastTargetOffset = n, this._clampedAdjustment = null, e.phase = ol(t, e.index != null && Pc(n, this.getScrollOffset())), this._scrollToOffset(n, { behavior: t }), this.scheduleScrollReconcile();
		}, this._readElementOffset = () => {
			let e = this.scrollElement;
			if (e == null) return null;
			let { horizontal: t, isRtl: n } = this.options;
			return typeof e.scrollTop == "number" ? t ? e.scrollLeft * (n && -1 || 1) : e.scrollTop : typeof e.scrollY == "number" ? t ? e.scrollX : e.scrollY : null;
		}, this._canWriteScroll = () => !(Uc() && this._iosGesture.inMotion()) && !this._reportedOverscroll, this._flushHoldIfReady = (e) => {
			if (this._holdOffset === 0 || !this.scrollElement || !this.options.enabled || !this._canWriteScroll() || this.scrollState?.phase === "smooth") return;
			this._holdOffset = 0, this.scrollOffset !== null && this.scrollOffset < 0 && (this.scrollOffset = 0), this.notify(e);
			let t = this.getCommandWrittenOffset() ?? this.getScrollOffset();
			this._clampedAdjustment = this._clampedBy(t), this._scrollToOffset(t);
		}, this.rafId = null, this.getSize = () => this.options.enabled ? (this.scrollRect = this.scrollRect ?? this.options.initialRect, this.scrollRect[this.options.horizontal ? "width" : "height"]) : (this.scrollRect = null, 0), this.getScrollOffset = () => this.options.enabled ? (this.scrollOffset = this.scrollOffset ?? (typeof this.options.initialOffset == "function" ? this.options.initialOffset() : this.options.initialOffset), this.scrollOffset) : (this.scrollOffset = null, 0), this.getMeasurementOptions = Mc(() => [
			this.options.count,
			this.options.paddingStart,
			this.options.scrollMargin,
			this.options.getItemKey,
			this.options.enabled,
			this.options.lanes,
			this.options.laneAssignmentMode,
			this.options.gap
		], (e, t, n, r, i, a, o, s) => (this.prevLanes !== void 0 && this.prevLanes !== a && (this.lanesChangedFlag = !0), this.prevLanes = a, this.pendingMin = null, {
			count: e,
			paddingStart: t,
			scrollMargin: n,
			getItemKey: r,
			enabled: i,
			lanes: a,
			laneAssignmentMode: o,
			gap: s
		}), { key: !1 }), this.isIndexInRange = (e) => e >= 0 && e < this.options.count, this.getMeasurements = Mc(() => [this.getMeasurementOptions(), this.itemSizeCacheVersion], ({ count: e, paddingStart: t, scrollMargin: n, getItemKey: r, enabled: i, lanes: a, laneAssignmentMode: o, gap: s }, c) => {
			let l = this.itemSizeCache;
			if (!i) return this.measurementsCache = [], this._singleLaneMeasurements = null, this.itemSizeCache.clear(), this.laneAssignments.clear(), [];
			if (this.laneAssignments.size > e) for (let t of this.laneAssignments.keys()) t >= e && this.laneAssignments.delete(t);
			this.lanesChangedFlag && (this.lanesChangedFlag = !1, this.lanesSettling = !0, this.measurementsCache = [], this._singleLaneMeasurements = null, this.itemSizeCache.clear(), this.laneAssignments.clear(), this.pendingMin = null), this.measurementsCache.length === 0 && !this.lanesSettling && (this.measurementsCache = this.options.initialMeasurementsCache, this.measurementsCache.forEach((e) => {
				this.itemSizeCache.set(e.key, e.size);
			}));
			let u = this.lanesSettling ? 0 : this.pendingMin ?? 0;
			if (this.pendingMin = null, this.lanesSettling && this.measurementsCache.length === e && (this.lanesSettling = !1), a === 1) {
				let i = e * 2, a = this._singleLaneMeasurements?.flat;
				if (!a || a.length < i) {
					let e = new Float64Array(i);
					a && u > 0 && e.set(a.subarray(0, u * 2)), a = e;
				}
				let o = u === 0 ? Array(e) : this._singleLaneMeasurements.items.slice(), c;
				if (u === 0) c = t + n;
				else {
					let e = u - 1;
					c = a[e * 2] + a[e * 2 + 1] + s;
				}
				for (let t = u; t < e; t++) {
					let e = r(t);
					o[t] = e;
					let n = l.get(e), i = typeof n == "number" ? n : this.options.estimateSize(t);
					a[t * 2] = c, a[t * 2 + 1] = i, c += i + s;
				}
				this._singleLaneMeasurements = {
					flat: a,
					items: o
				};
				let d = jc(o, a);
				return this.measurementsCache = d, d;
			}
			let d = this.measurementsCache.slice(0, u), f = Array(a).fill(void 0), p = new Float64Array(a), m = 0;
			for (let e = 0; e < u; e++) {
				let t = d[e];
				t && (f[t.lane] === void 0 && m++, f[t.lane] = e, p[t.lane] = t.end);
			}
			for (let i = u; i < e; i++) {
				let e = r(i), c = this.laneAssignments.get(i), u, h, g = o === "estimate" || l.has(e);
				if (c !== void 0 && this.options.lanes > 1) {
					u = c;
					let e = f[u], r = e === void 0 ? void 0 : d[e];
					h = r ? r.end + s : t + n;
				} else if (m === a) {
					let e = 0, t = p[0], n = f[0];
					for (let r = 1; r < a; r++) {
						let i = p[r];
						(i < t || i === t && f[r] < n) && (e = r, t = i, n = f[r]);
					}
					u = e, h = t + s, g && this.laneAssignments.set(i, u);
				} else u = i % this.options.lanes, h = t + n, g && this.laneAssignments.set(i, u);
				let _ = l.get(e), v = typeof _ == "number" ? _ : this.options.estimateSize(i), y = h + v;
				d[i] = {
					index: i,
					start: h,
					size: v,
					end: y,
					key: e,
					lane: u
				}, f[u] === void 0 && m++, f[u] = i, p[u] = y;
			}
			return this.measurementsCache = d, d;
		}, {
			key: !1,
			debug: () => this.options.debug
		}), this.calculateRange = Mc(() => [
			this.getMeasurements(),
			this.getSize(),
			this.getScrollOffset(),
			this.options.lanes,
			this._getLayoutTotalSize() + this.options.scrollMargin
		], (e, t, n, r, i) => {
			if (e.length === 0 || t === 0) return this.range = null, null;
			let a = Math.max(i - t, 0);
			return this.range = fl(e, t, Math.min(n, a), r, r === 1 && this._singleLaneMeasurements !== null ? this._singleLaneMeasurements.flat : null), this.range;
		}, {
			key: !1,
			debug: () => this.options.debug
		}), this.getVirtualIndexes = Mc(() => {
			let e = null, t = null, n = this.calculateRange();
			return n && (e = n.startIndex, t = n.endIndex), this.maybeNotify.updateDeps([
				this.isScrolling,
				e,
				t
			]), [
				this.options.rangeExtractor,
				this.options.overscan,
				this.options.count,
				e,
				t,
				this.options.lanes
			];
		}, (e, t, n, r, i, a) => r === null || i === null ? [] : e({
			startIndex: r,
			endIndex: i,
			overscan: t,
			count: n,
			lanes: a
		}), {
			key: !1,
			debug: () => this.options.debug
		}), this.indexFromElement = (e) => {
			let t = this.options.indexAttribute, n = e.getAttribute(t);
			return n ? parseInt(n, 10) : (console.warn(`Missing attribute name '${t}={index}' on measured element.`), -1);
		}, this.shouldMeasureDuringScroll = (e) => {
			if (!sl(this.scrollState)) return !0;
			let t = this.scrollState.index ?? this.getVirtualItemForOffset(this.scrollState.lastTargetOffset)?.index;
			if (t !== void 0 && this.range) {
				let n = Math.max(this.options.overscan, Math.ceil((this.range.endIndex - this.range.startIndex) / 2)), r = Math.max(0, t - n), i = Math.min(this.options.count - 1, t + n);
				return e >= r && e <= i;
			}
			return !0;
		}, this.measureElement = (e) => {
			if (!e) {
				this.elementsCache.forEach((e, t) => {
					e.isConnected || (this.observer.unobserve(e), this.elementsCache.delete(t));
				});
				return;
			}
			let t = this.indexFromElement(e);
			if (!this.isIndexInRange(t)) return;
			let n = this.options.getItemKey(t), r = this.elementsCache.get(n);
			r !== e && (r && this.observer.unobserve(r), this.observer.observe(e), this.elementsCache.set(n, e)), e.isConnected && (!this.isScrolling || this.scrollState) && this.shouldMeasureDuringScroll(t) && this.resizeItem(t, this.options.measureElement(e, void 0, this));
		}, this.resizeItem = (e, t) => {
			var n;
			if (!this.isIndexInRange(e)) return;
			this._resizeBatch?.sync && this.getMeasurements();
			let r, i, a, o = this._singleLaneMeasurements?.flat;
			if (this.options.lanes === 1 && o != null) a = this.options.getItemKey(e), i = o[e * 2], r = o[e * 2 + 1];
			else {
				let t = this.measurementsCache[e];
				if (!t) return;
				a = t.key, i = t.start, r = t.size;
			}
			let s = this.itemSizeCache.get(a) ?? r, c = t - s;
			if (c !== 0) {
				let o = this.options.anchorTo === "end" && !sl(this.scrollState) && this.getVirtualDistanceFromEnd() <= this.options.scrollEndThreshold, l = o ? this._getLayoutTotalSize() : 0, u = this.getScrollOffset(), d = this.itemSizeCache.has(a) ? i + s <= u && this.scrollDirection !== "backward" : i < u, f = !sl(this.scrollState) && (this.shouldAdjustScrollPositionOnItemSizeChange === void 0 ? d : this.shouldAdjustScrollPositionOnItemSizeChange(this.measurementsCache[e] ?? {
					index: e,
					key: a,
					start: i,
					size: r,
					end: i + r,
					lane: 0
				}, c, this));
				(this.pendingMin === null || e < this.pendingMin) && (this.pendingMin = e), this.itemSizeCache.set(a, t), this.itemSizeCacheVersion++;
				let p = !1;
				if (this._awaitingCommandReadBack() || (o ? p = this.applyScrollAdjustment(this._getLayoutTotalSize() - l) : f && (p = this.applyScrollAdjustment(c))), this._resizeBatch) {
					this._resizeBatch.notify = !0, (n = this._resizeBatch).sync || (n.sync = p);
					return;
				}
				this.notify(p), this._noteClampMax(), this._flushHoldIfReady(!0), this._retryClampedAdjustment();
			}
		}, this.getVirtualItems = Mc(() => [
			this.getVirtualIndexes(),
			this.getMeasurements(),
			this._holdOffset
		], (e, t, n) => {
			let r = [];
			for (let i = 0, a = e.length; i < a; i++) {
				let a = t[e[i]];
				r.push(n === 0 ? a : {
					...a,
					start: a.start - n,
					end: a.end - n
				});
			}
			return r;
		}, {
			key: !1,
			debug: () => this.options.debug
		}), this.getVirtualItemForOffset = (e) => {
			let t = this.getMeasurements();
			if (t.length === 0) return;
			let n = this._singleLaneMeasurements?.flat, r = this.options.lanes === 1 && n != null;
			return Nc(t[ul(0, t.length - 1, r ? (e) => n[e * 2] : (e) => Nc(t[e]).start, e)]);
		}, this._noteClampMax = () => {
			if (!this._awaitingCommandReadBack()) return;
			let e = this._elementMaxScrollOffset();
			if (e === null) return;
			let t = this.pendingWrite, n = Math.max(e, 0) + this._holdOffset;
			if (n >= t.target) return;
			let r = t.lowMaxes ??= [];
			r[r.length - 1] !== n && r.push(n);
		}, this.getMaxScrollOffset = () => (this._elementMaxScrollOffset() ?? 0) + this._holdOffset, this._toLayoutOffset = (e) => {
			let t = e + this._holdOffset;
			return this.pendingScrollAnchor?.kind === "anchor" && (t += this.pendingScrollAnchor.delta), t;
		}, this._elementMaxScrollOffset = () => {
			let e = this.scrollElement;
			if (e === null) return null;
			if ("scrollHeight" in e) return this.options.horizontal ? e.scrollWidth - e.clientWidth : e.scrollHeight - e.clientHeight;
			if ("document" in e) {
				let t = e.document.documentElement;
				return this.options.horizontal ? t.scrollWidth - e.innerWidth : t.scrollHeight - e.innerHeight;
			}
			return null;
		}, this._momentumStop = null, this._stopMomentum = (e) => {
			let t = this.scrollElement, n = this.targetWindow;
			if (t === null || n === null) return;
			let r = "document" in t ? t.document.scrollingElement : t;
			if (!r?.style) return;
			if (this._momentumStop) {
				this._momentumStop.done = e;
				return;
			}
			let { style: i } = r, a = this.options.horizontal ? "overflow-x" : "overflow-y", o = i.getPropertyValue(a), s = i.getPropertyPriority(a), c = () => this.options.horizontal ? r.clientHeight : r.clientWidth, l = c();
			if (i.setProperty(a, "hidden", "important"), c() !== l) {
				i.setProperty(a, o, s);
				return;
			}
			let u = {
				restore: () => i.getPropertyValue(a) !== "hidden" || i.getPropertyPriority(a) !== "important" ? !1 : (i.setProperty(a, o, s), !0),
				done: e
			};
			this._momentumStop = u, n.requestAnimationFrame(() => n.requestAnimationFrame(() => {
				this._momentumStop === u && (this._momentumStop = null, u.restore() && u.done());
			}));
		}, this._clampedBy = (e) => {
			let t = this._elementMaxScrollOffset();
			if (t === null) return null;
			let n = t + this._holdOffset;
			return e > n + .5 ? {
				target: e,
				maxAtWrite: n
			} : null;
		}, this.getVirtualDistanceFromEnd = () => Math.max(this._getLayoutTotalSize() + this.options.scrollMargin - this.getSize() - this.getScrollOffset(), 0), this.getDistanceFromEnd = () => Math.max(this.getMaxScrollOffset() - this.getScrollOffset(), 0), this.isAtEnd = (e = this.options.scrollEndThreshold) => this.getDistanceFromEnd() <= e, this.getOffsetForAlignment = (e, t, n = 0) => {
			if (!this.scrollElement) return 0;
			let r = this.getSize(), i = this.getScrollOffset();
			t === "auto" && (t = e >= i + r ? "end" : "start"), t === "center" ? e += (n - r) / 2 : t === "end" && (e -= r);
			let a = this.getMaxScrollOffset();
			return Math.max(Math.min(a, e), 0);
		}, this.getOffsetForIndex = (e, t = "auto") => {
			e = Math.max(0, Math.min(e, this.options.count - 1));
			let n = this.getSize(), r = this.getScrollOffset(), i = this.getMeasurements()[e];
			if (!i) return;
			if (t === "auto") {
				if (i.end >= r + n - this.options.scrollPaddingEnd) t = "end";
				else if (i.start <= r + this.options.scrollPaddingStart) t = "start";
				else return [r, t];
			}
			if (t === "end" && e === this.options.count - 1) {
				let e = this.getMaxScrollOffset(), n = e - this.options.paddingEnd + this.options.scrollPaddingEnd;
				return [Math.max(Math.min(n, e), 0), t];
			}
			let a = t === "end" ? i.end + this.options.scrollPaddingEnd : i.start - this.options.scrollPaddingStart;
			return [this.getOffsetForAlignment(a, t, i.size), t];
		}, this.scrollToOffset = (e, { align: t = "start", behavior: n = "auto" } = {}) => {
			this._startScroll({
				index: null,
				align: t
			}, this.getOffsetForAlignment(e, t), n, "absolute");
		}, this.scrollToIndex = (e, { align: t = "auto", behavior: n = "auto" } = {}) => {
			e = Math.max(0, Math.min(e, this.options.count - 1));
			let r = this.getOffsetForIndex(e, t);
			if (!r) return;
			let [i, a] = r;
			this._startScroll({
				index: e,
				align: a
			}, i, n, "absolute");
		}, this.scrollBy = (e, { behavior: t = "auto" } = {}) => {
			let n = this.getOffsetForAlignment((this.getCommandWrittenOffset() ?? this.getScrollOffset()) + e, "start");
			this._startScroll({
				index: null,
				align: "start"
			}, n, t, "relative");
		}, this.scrollToEnd = ({ behavior: e = "auto" } = {}) => {
			this.options.count > 0 ? this._startScroll({
				index: this.options.count - 1,
				align: "end",
				toEnd: !0
			}, Math.max(this.getMaxScrollOffset(), 0), e, "absolute") : this.scrollToOffset(Math.max(this.getTotalSize() - this.getSize(), 0), { behavior: e });
		}, this.cancelScroll = () => {
			this.rafId != null && this.targetWindow && (this.targetWindow.cancelAnimationFrame(this.rafId), this.rafId = null), this.scrollState = null;
		}, this.getTotalSize = () => Math.max(this._getLayoutTotalSize() - this._holdOffset, 0), this._getLayoutTotalSize = () => {
			let e = this.getMeasurements(), t;
			if (e.length === 0) t = this.options.paddingStart;
			else if (this.options.lanes === 1) {
				let n = e.length - 1, r = this._singleLaneMeasurements?.flat;
				t = r == null ? e[n]?.end ?? 0 : r[n * 2] + r[n * 2 + 1];
			} else {
				let n = Array(this.options.lanes).fill(null), r = e.length - 1;
				for (; r >= 0 && n.some((e) => e === null);) {
					let t = e[r];
					n[t.lane] === null && (n[t.lane] = t.end), r--;
				}
				t = Math.max(...n.filter((e) => e !== null));
			}
			return Math.max(t - this.options.scrollMargin + this.options.paddingEnd, 0);
		}, this.takeSnapshot = () => {
			let e = [];
			if (this.itemSizeCache.size === 0) return e;
			let t = this.getMeasurements();
			for (let n of t) n && this.itemSizeCache.has(n.key) && e.push({
				index: n.index,
				key: n.key,
				start: n.start,
				size: n.size,
				end: n.end,
				lane: n.lane
			});
			return e;
		}, this._scrollToOffset = (e, { adjustments: t, behavior: n } = {}) => {
			let r = {
				target: e + (t ?? 0),
				smooth: n === "smooth"
			};
			return this.pendingWrite = r, this.options.scrollToFn(e - this._holdOffset, {
				behavior: n,
				adjustments: t
			}, this), r;
		}, this.getCommandWrittenOffset = () => this.scrollState !== null && this.pendingWrite !== null && !this.pendingWrite.smooth ? this.pendingWrite.target : null, this._awaitingCommandReadBack = () => {
			let e = this.getCommandWrittenOffset();
			return !(Uc() && this._iosGesture.dragging()) && e !== null && !Pc(e, this.getScrollOffset());
		}, this._followOffsetTarget = (e) => {
			this.scrollState !== null && this.scrollState.index == null && (this.scrollState.lastTargetOffset += e);
		}, this.measure = () => {
			this.pendingMin = null, this.itemSizeCache.clear(), this.laneAssignments.clear(), this.itemSizeCacheVersion++, this.notify(!1);
		}, this.setOptions(e);
	}
	get scrollAdjustments() {
		return 0;
	}
	applyScrollAdjustment(e, t) {
		if (e === 0) return !1;
		if (this._followOffsetTarget(e), this._canWriteScroll()) {
			let n = this.getScrollOffset();
			this._clampedAdjustment = this._clampedBy(n + e);
			let r = this._scrollToOffset(n, {
				adjustments: e,
				behavior: t
			});
			return this.scrollOffset !== null && (this.pendingWrite === r && (this.scrollOffset += e), this.scrollOffset < 0 && (this.scrollOffset = 0)), !0;
		}
		return this._holdOffset += e, this.scrollOffset !== null && (this.scrollOffset += e), !0;
	}
	scheduleScrollReconcile() {
		this.targetWindow ? this.rafId ??= this.targetWindow.requestAnimationFrame(() => {
			this.rafId = null, this.reconcileScroll();
		}) : this.scrollState = null;
	}
	getScrollStateTarget(e) {
		let t = e.toEnd ? [Math.max(this.getMaxScrollOffset(), 0)] : e.index == null ? void 0 : this.getOffsetForIndex(e.index, e.align);
		return t ? t[0] : e.lastTargetOffset;
	}
	reconcileScroll() {
		if (!this.scrollState || !this.scrollElement) return;
		if (this.now() - this.scrollState.startedAt > 5e3) {
			this.scrollState = null;
			return;
		}
		if (this.scrollState.phase === "queued") {
			this.scheduleScrollReconcile();
			return;
		}
		this.getMeasurements();
		let e = this.getScrollStateTarget(this.scrollState), t = this.scrollState.index == null ? 1 : this.options.useAnimationFrameWithResizeObserver ? 3 : 2, n = e !== this.scrollState.lastTargetOffset;
		if (!n && Pc(e, this.getScrollOffset())) {
			if (this.scrollState.phase === "smooth" ? this.scrollState.phase = "smoothLanded" : this.scrollState.phase === "smoothFinishing" && (this.scrollState.phase = "instant"), this.scrollState.stableFrames++, this.scrollState.stableFrames >= t) {
				this.getScrollOffset() !== e && this._scrollToOffset(e, { behavior: "auto" }), this.scrollState = null;
				return;
			}
		} else if (this.scrollState.stableFrames = 0, n) {
			let t = this.getSize() || 600, n = Math.abs(e - this.getScrollOffset()), r = sl(this.scrollState) && n > t;
			this.scrollState.lastTargetOffset = e, r ? this.scrollState.phase = "smooth" : this.scrollState.phase === "smooth" ? this.scrollState.phase = "smoothFinishing" : this.scrollState.phase === "smoothLanded" && (this.scrollState.phase = "instant"), this._scrollToOffset(e, { behavior: r ? "smooth" : "auto" });
		}
		this.scheduleScrollReconcile();
	}
	_startScroll(e, t, n, r) {
		this.pendingScrollAnchor = null, this._clampedAdjustment = null;
		let i = this._iosGesture.tail();
		if (r === "absolute" && i !== null) {
			if (i === "fling" && Kc()) {
				this._stopMomentum(() => {
					this.scrollState?.phase !== "queued" || this._iosGesture.tail() !== "fling" || this._reportedOverscroll || (this._iosGesture.end(), this._flushHoldIfReady(!0), this._writeAwaitingCommand());
				}), this.scrollState = {
					...e,
					startedAt: this.now(),
					lastTargetOffset: t,
					stableFrames: 0,
					phase: "queued",
					behavior: n,
					samples: []
				}, this.scheduleScrollReconcile();
				return;
			}
			this._iosGesture.end();
		}
		this.scrollState = {
			...e,
			startedAt: this.now(),
			lastTargetOffset: t,
			stableFrames: 0,
			behavior: n,
			phase: ol(n, e.index != null && Pc(t, this.getScrollOffset()))
		}, this._scrollToOffset(t, { behavior: n }), this.scheduleScrollReconcile();
	}
}, ul = (e, t, n, r) => {
	for (; e <= t;) {
		let i = (e + t) / 2 | 0, a = n(i);
		if (a < r) e = i + 1;
		else if (a > r) t = i - 1;
		else return i;
	}
	return e > 0 ? e - 1 : 0;
};
function dl(e, t, n) {
	let r = 0;
	for (; r <= t;) {
		let i = (r + t) / 2 | 0, a = e[i * 2];
		if (a < n) r = i + 1;
		else if (a > n) t = i - 1;
		else return i;
	}
	return r > 0 ? r - 1 : 0;
}
function fl(e, t, n, r, i) {
	let a = e.length - 1;
	if (e.length <= r) return {
		startIndex: 0,
		endIndex: a
	};
	if (r === 1 && i !== null) {
		let e = dl(i, a, n), r = e, o = n + t;
		for (; r < a && i[r * 2] + i[r * 2 + 1] < o;) r++;
		return {
			startIndex: e,
			endIndex: r
		};
	}
	let o = ul(0, a, (t) => e[t].start, n), s = o;
	if (r === 1) for (; s < a && e[s].end < n + t;) s++;
	else if (r > 1) {
		let i = Array(r).fill(0);
		for (; s < a && i.some((e) => e < n + t);) {
			let t = e[s];
			i[t.lane] = t.end, s++;
		}
		let c = Array(r).fill(n + t);
		for (; o >= 0 && c.some((e) => e >= n);) {
			let t = e[o];
			c[t.lane] = t.start, o--;
		}
		o = Math.max(0, o - o % r), s = Math.min(a, s + (r - 1 - s % r));
	}
	return {
		startIndex: o,
		endIndex: s
	};
}
// frontend/session/viewport.ts
function pl(e) {
	let t = [...e.querySelectorAll(":scope > .tl")], n = document.createElement("div");
	n.className = "trace-items";
	let r = document.createElement("div"), i = [], a = [], o = /* @__PURE__ */ new Set(), s = /* @__PURE__ */ new WeakMap(), c = /* @__PURE__ */ new Set(), l = 48, u = 0, d = 0, f = 0, p, m = !1, h = !1, g = (e) => {
		let t = s.get(e);
		return t || (t = JSON.stringify([
			d,
			e.dataset.historyId || `local-${++u}`,
			e.dataset.agent || "",
			e.dataset.taskPart || e.dataset.eventType || "",
			e.className
		]), s.set(e, t)), t;
	}, _ = new MutationObserver((e) => {
		let t = /* @__PURE__ */ new Set();
		for (let n of e) {
			let e = (n.target instanceof Element ? n.target : n.target.parentElement)?.closest(".tl");
			e && o.has(e) && t.add(e);
		}
		t.size && (c.forEach((e) => e([...t])), b());
	}), v = new ll({
		count: 0,
		getScrollElement: () => e,
		estimateSize: () => 180,
		getItemKey: (e) => g(a[e]),
		observeElementRect: $c,
		observeElementOffset: rl,
		scrollToFn: al,
		anchorTo: "end",
		followOnAppend: !0,
		scrollEndThreshold: 24,
		overscan: 6,
		paddingStart: 24,
		paddingEnd: 48,
		onChange: () => y()
	});
	v.shouldAdjustScrollPositionOnItemSizeChange = (e, t, n) => {
		let r = n.scrollOffset ?? 0;
		return n.itemSizeCache.has(e.key) ? e.end <= r : e.start < r;
	};
	function y() {
		if (m) h = !0;
		else {
			m = !0;
			try {
				n.style.height = `${v.getTotalSize()}px`;
				let e = v.getVirtualItems(), t = new Set(e.map((e) => a[e.index]));
				for (let e of [...n.children]) t.has(e) || e.remove();
				for (let [t, r] of e.entries()) {
					let e = a[r.index];
					e.dataset.index = String(r.index), e.style.transform = `translate(-50%, ${r.start}px)`, n.children[t] !== e && n.insertBefore(e, n.children[t] || null), v.measureElement(e);
				}
				n.style.height = `${v.getTotalSize()}px`;
				let r = v.getVirtualIndexes();
				(r.length !== e.length || r[0] !== e[0]?.index || r.at(-1) !== e.at(-1)?.index) && (h = !0);
			} finally {
				m = !1;
			}
			h && (h = !1, queueMicrotask(y));
		}
	}
	function b() {
		let e = i.filter((e) => !e.hidden && !e.classList.contains("reader-filtered"));
		for (let t of e) {
			let e = t.matches(".tl-conv,.tl-task-step,.tl-turn");
			t.toggleAttribute("data-reader-separated", e);
		}
		a = e;
		let t = e.map(g);
		v.setOptions({
			...v.options,
			count: e.length,
			paddingEnd: l,
			getItemKey: (e) => t[e]
		}), y(), v._willUpdate(), y();
	}
	function x(e) {
		_.observe(e, {
			childList: !0,
			subtree: !0,
			characterData: !0,
			attributes: !0,
			attributeFilter: [
				"hidden",
				"data-task-status",
				"data-task-part",
				"data-event-type"
			]
		});
	}
	function S(t, n) {
		if (n === "replace") w();
		else if (n === "prepend" && a.length) {
			let t = e.scrollTop + e.clientHeight - v.getTotalSize();
			l += Math.max(0, Math.ceil(t));
		}
		for (let e of t) g(e), o.add(e), x(e);
		i = n === "prepend" ? [...t, ...i] : [...i, ...t], c.forEach((e) => e(t)), b();
	}
	function C(e) {
		o.add(e), x(e), i.push(e), p === void 0 && (p = requestAnimationFrame(() => {
			p = void 0, c.forEach((t) => t([e])), b();
		}));
	}
	function w() {
		p !== void 0 && cancelAnimationFrame(p), p = void 0, d++, _.disconnect(), l = 48, i = [], a = [], o.clear(), r.replaceChildren(), ++f, v.cancelScroll(), v.setOptions({
			...v.options,
			count: 0,
			getItemKey: () => ""
		}), v.measure(), b(), c.forEach((e) => e([]));
	}
	let T = v._didMount();
	return e.classList.add("trace-virtual"), e.replaceChildren(n), v._willUpdate(), t.length && S(t, "replace"), {
		sink: r,
		rows: () => i,
		mounted: () => [...n.children],
		has: (e) => !!e && o.has(e),
		append: C,
		commit: S,
		clear: w,
		refresh: b,
		subscribe(e) {
			return c.add(e), () => {
				c.delete(e);
			};
		},
		toEnd() {
			l = 48, b(), v.scrollToEnd({ behavior: "instant" });
		},
		stop() {
			++f, v.cancelScroll();
		},
		isAtEnd: () => v.isAtEnd(),
		async navigate(e, t = "start") {
			let n = ++f;
			b();
			let r = a.indexOf(e);
			return r < 0 || (v.scrollToIndex(r, {
				align: t,
				behavior: "instant"
			}), await new Promise(requestAnimationFrame), n !== f || !o.has(e)) ? !1 : (y(), e.tabIndex = -1, e.focus({ preventScroll: !0 }), e.isConnected);
		},
		destroy() {
			w(), T(), c.clear();
		}
	};
}
// frontend/session/main.tsx
function ml() {
	return /* @__PURE__ */ (0, O.jsxs)("svg", {
		width: "15",
		height: "15",
		viewBox: "0 0 24 24",
		fill: "none",
		stroke: "currentColor",
		strokeWidth: "1.7",
		"aria-hidden": "true",
		children: [/* @__PURE__ */ (0, O.jsx)("circle", {
			cx: "10.5",
			cy: "10.5",
			r: "6.5"
		}), /* @__PURE__ */ (0, O.jsx)("path", { d: "m16 16 5 5" })]
	});
}
function hl({ bridge: e }) {
	let t = (0, _.useSyncExternalStore)(e.subscribe, e.getSnapshot), [n, r] = (0, _.useState)({
		sessionId: t.sessionId,
		query: ""
	}), i = n.sessionId === t.sessionId ? n : {
		sessionId: t.sessionId,
		query: ""
	}, { query: a } = i, [o, s] = (0, _.useState)(!1), c = t.language.startsWith("en"), l = (e, t) => c ? t : e, u = {
		message: l("对话与要求", "Conversation and requests"),
		result: l("结果与交付", "Results and deliverables"),
		attention: l("需要关注", "Needs attention"),
		progress: l("任务进展", "Task progress"),
		activity: l("运行记录", "Activity")
	}, d = t.entries.filter((e) => Ec(e, a));
	(0, _.useEffect)(() => {
		e.filter(a);
	}, [
		e,
		t.sessionId,
		a
	]), (0, _.useEffect)(() => {
		s(!1);
	}, [t.sessionId]);
	function f(t) {
		s(!1), e.navigate(t);
	}
	return /* @__PURE__ */ (0, O.jsxs)(As, {
		appearance: t.appearance,
		accentColor: "bronze",
		grayColor: "sand",
		radius: "medium",
		scaling: "100%",
		className: "session-reader-theme",
		children: [/* @__PURE__ */ (0, O.jsxs)("div", {
			className: "reader-toolbar",
			"aria-label": l("会话阅读工具", "Conversation reading tools"),
			children: [/* @__PURE__ */ (0, O.jsx)("div", {
				className: "reader-leading",
				children: /* @__PURE__ */ (0, O.jsx)(bs, {
					size: "1",
					color: "gray",
					className: "reader-count",
					role: "status",
					children: a ? `${d.length} / ${t.entries.length}` : l(`${t.entries.length} 条记录`, `${t.entries.length} entries`)
				})
			}), /* @__PURE__ */ (0, O.jsxs)("div", {
				className: "reader-controls",
				children: [/* @__PURE__ */ (0, O.jsxs)(Sc, {
					size: "1",
					value: a,
					type: "search",
					placeholder: l("查找当前记录", "Find in loaded history"),
					"aria-label": l("查找已加载的会话记录", "Search loaded conversation history"),
					onChange: (e) => r({
						...i,
						query: e.target.value
					}),
					children: [/* @__PURE__ */ (0, O.jsx)(Cc, { children: /* @__PURE__ */ (0, O.jsx)(ml, {}) }), a && /* @__PURE__ */ (0, O.jsx)(Cc, { children: /* @__PURE__ */ (0, O.jsx)(fc, {
						size: "1",
						variant: "ghost",
						"aria-label": l("清除搜索", "Clear search"),
						onClick: () => r({
							...i,
							query: ""
						}),
						children: "×"
					}) })]
				}), /* @__PURE__ */ (0, O.jsxs)(hc, {
					open: o,
					onOpenChange: s,
					children: [/* @__PURE__ */ (0, O.jsx)(gc, { children: /* @__PURE__ */ (0, O.jsxs)(dc, {
						size: "1",
						variant: "ghost",
						className: "reader-outline-trigger",
						children: [l("目录", "Outline"), /* @__PURE__ */ (0, O.jsx)("span", {
							"aria-hidden": "true",
							children: "☷"
						})]
					}) }), /* @__PURE__ */ (0, O.jsxs)(_c, {
						className: "reader-outline",
						size: "2",
						align: "end",
						style: {
							width: 420,
							maxWidth: "calc(100vw - 32px)"
						},
						children: [
							/* @__PURE__ */ (0, O.jsx)(bs, {
								as: "p",
								size: "2",
								weight: "medium",
								children: l("阅读目录", "Reading outline")
							}),
							/* @__PURE__ */ (0, O.jsx)("p", {
								className: "reader-outline-note",
								children: l("按内容分类，仅索引已加载记录。", "Grouped by content; loaded history only.")
							}),
							/* @__PURE__ */ (0, O.jsxs)("nav", {
								className: "reader-outline-list",
								"aria-label": l("会话内容目录", "Conversation outline"),
								children: [Oc(d).map((e) => /* @__PURE__ */ (0, O.jsxs)("section", {
									className: "reader-outline-section",
									children: [/* @__PURE__ */ (0, O.jsxs)("h3", { children: [u[e.kind], /* @__PURE__ */ (0, O.jsx)("span", { children: e.entries.length })] }), e.entries.map((e) => /* @__PURE__ */ (0, O.jsxs)("button", {
										type: "button",
										title: e.title,
										onClick: () => f(e.id),
										children: [
											/* @__PURE__ */ (0, O.jsx)("span", {
												className: `reader-entry-dot ${e.kind}`,
												"aria-hidden": "true"
											}),
											/* @__PURE__ */ (0, O.jsxs)("span", {
												className: "reader-outline-copy",
												children: [/* @__PURE__ */ (0, O.jsx)("span", { children: e.title }), /* @__PURE__ */ (0, O.jsx)("small", { children: e.actor })]
											}),
											/* @__PURE__ */ (0, O.jsx)("time", { children: e.time })
										]
									}, e.id))]
								}, e.kind)), !d.length && /* @__PURE__ */ (0, O.jsx)(bs, {
									as: "p",
									size: "2",
									color: "gray",
									children: l("没有匹配的记录", "No matching entries")
								})]
							})
						]
					})]
				})]
			})]
		}), a && !d.length && /* @__PURE__ */ (0, O.jsxs)("div", {
			className: "reader-empty",
			role: "status",
			children: [l("当前已加载的记录中没有匹配内容。", "No matches in the currently loaded history."), /* @__PURE__ */ (0, O.jsx)("button", {
				type: "button",
				onClick: () => r({
					...i,
					query: ""
				}),
				children: l("显示全部记录", "Show all entries")
			})]
		})]
	});
}
var gl = document.querySelector("#session-reader");
if (gl) {
	let e = document.querySelector("#trace-box");
	e && (window.TraceViewport = pl(e));
	let t = kc();
	(0, wc.createRoot)(gl).render(/* @__PURE__ */ (0, O.jsx)(hl, { bridge: t }));
}
