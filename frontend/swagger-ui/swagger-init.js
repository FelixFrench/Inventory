window.onload = function () {
    SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        presets: [
            SwaggerUIBundle.presets.apis,
            SwaggerUIBundle.SwaggerUIStandalonePreset,
        ],
        layout: 'StandaloneLayout',
        deepLinking: true,
    });
};
