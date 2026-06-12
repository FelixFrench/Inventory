window.onload = function () {
    SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        presets: [
            SwaggerUIBundle.presets.apis,
        ],
        deepLinking: true,
    });
};
