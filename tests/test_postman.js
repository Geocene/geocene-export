// Run with: node tests/test_postman.js. No API calls or extra packages.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const directory = path.join(__dirname, '..', 'postman');
const collection = JSON.parse(fs.readFileSync(path.join(directory, 'geocene-export.postman_collection.json')));
const template = JSON.parse(fs.readFileSync(path.join(directory, 'geocene-export.postman_environment.json')));

function environment(overrides = {}) {
    return Object.assign(Object.fromEntries(template.values.map(row => [row.key, row.value])), {
        token: 'offline-test-token', organization_id: '123'
    }, overrides);
}

// Execute the actual collection scripts against an in-memory Postman API.
function request(number, values) {
    const item = collection.item.find(row => row.name.startsWith(number + '.'));
    const local = {};
    const result = {skipped: false, query: new Map()};
    const pm = {
        environment: {
            get: key => values[key],
            set: (key, value) => { values[key] = value; },
            unset: key => { delete values[key]; }
        },
        variables: {
            get: key => Object.hasOwn(local, key) ? local[key] : values[key],
            set: (key, value) => { local[key] = value; }
        },
        info: {requestName: item.name},
        execution: {skipRequest: () => { result.skipped = true; }},
        request: {url: {query: {
            remove: key => result.query.delete(key),
            upsert: row => result.query.set(row.key, row.value)
        }}}
    };
    function run(owner, event) {
        for (const hook of owner.event || []) {
            if (hook.listen === event) {
                vm.runInNewContext('(function () {\n' + hook.script.exec.join('\n') + '\n})()', {pm});
            }
        }
    }
    result.send = () => {
        run(collection, 'prerequest');
        run(item, 'prerequest');
        return result;
    };
    result.respond = (body, code = 200) => {
        pm.response = {code, json: () => body};
        run(item, 'test');
        return result;
    };
    result.body = () => JSON.parse(item.request.body.raw.replace(/\{\{([^}]+)}}/g,
        (_, key) => String(pm.variables.get(key))));
    return result;
}

function select(values, ids = ['mission-a']) {
    return request(4, values).send().respond({results: {id: ids}});
}

test('distributed credentials and state are blank; metrics default to false', () => {
    const values = Object.fromEntries(template.values.map(row => [row.key, row.value]));
    for (const key of ['username', 'password', 'token', 'job_id', 'download_url']) {
        assert.equal(values[key], '');
    }
    assert.equal(String(values.include_metrics), 'false');
    assert.equal(collection.auth.type, 'apikey');
    assert.ok(collection.auth.apikey.some(row => row.key === 'value' && row.value === 'Token {{token}}'));
    for (const item of collection.item) {
        assert.equal(item.protocolProfileBehavior.followRedirects, false);
    }
});

test('login safely serializes credentials, stores token, and clears password', () => {
    const values = environment({username: 'you@example.com', password: 'quoted"\\password'});
    const login = request(1, values).send();
    assert.deepEqual(login.body(), {username: 'you@example.com', password: 'quoted"\\password'});
    login.respond({token: 'new-test-token'});
    assert.equal(values.token, 'new-test-token');
    assert.equal(values.password, undefined);
});

test('missing setup and unexpected API host stop before sending credentials', () => {
    for (const overrides of [{token: ''}, {base_url: 'https://example.invalid'}]) {
        const operation = request(2, environment(overrides));
        assert.throws(operation.send);
        assert.equal(operation.skipped, true);
    }
});

test('empty selection clears stale missions and prevents creation', () => {
    const values = environment({mission_ids: '["old"]', selection_scope: '["123", ""]'});
    assert.throws(() => select(values, []), /No valid missions/);
    const create = request(5, values);
    assert.throws(create.send, /Select missions/);
    assert.equal(create.skipped, true);
});

test('tag selection is optional and scope changes require selecting again', () => {
    const values = environment({tag_id: '17'});
    const selection = select(values);
    assert.equal(selection.query.get('tags'), '17');
    values.organization_id = '124';
    const create = request(5, values);
    assert.throws(create.send, /changed/);
    assert.equal(create.skipped, true);
    values.tag_id = '';
    assert.equal(select(values).query.has('tags'), false);
});

test('creation produces real JSON booleans and preserves an ambiguous submission', () => {
    const values = environment({include_sensors: 'false', include_tags: 'false'});
    select(values);
    const create = request(5, values).send();
    const body = create.body();
    assert.deepEqual(body.missions, ['mission-a']);
    assert.deepEqual(body.filters, {organization: 123, metrics: false, events: true,
        sensors: false, tags: false, mission_logs: true});
    assert.equal(values.job_id, 'unknown');
    const repeat = request(5, values);
    assert.throws(repeat.send, /already exist/);
    assert.equal(repeat.skipped, true);
    create.respond({id: 456}, 201);
    assert.equal(values.job_id, '456');
});

test('invalid data options stop before creating a job', () => {
    const values = environment({include_metrics: 'yes'});
    select(values);
    const create = request(5, values);
    assert.throws(create.send, /true or false/);
    assert.equal(create.skipped, true);
    assert.equal(values.job_id, '');
});

test('progress 100 while processing clears stale download state', () => {
    const values = environment({job_id: '456', job_status: 'success',
        download_url: 'https://example.invalid/old.zip', download_job_id: '456'});
    request(6, values).send().respond({status: 'processing', progress: 100});
    assert.equal(values.download_url, undefined);
    const download = request(7, values);
    assert.throws(download.send, /Check export/);
    assert.equal(download.skipped, true);
});

test('failed jobs and unsafe download URLs cannot become downloadable', () => {
    for (const response of [{status: 'error'}, {status: 'success', download_url: 'http://example.invalid/a.zip'},
        {status: 'success', download_url: 'https://user:pass@example.invalid/a.zip'}]) {
        const values = environment({job_id: '456'});
        assert.throws(() => request(6, values).send().respond(response));
        const download = request(7, values);
        assert.throws(download.send);
        assert.equal(download.skipped, true);
    }
});

test('downloads require checked success for the same job and have no API auth', () => {
    const values = environment({job_id: '456'});
    request(6, values).send().respond({status: 'success', download_url: 'https://example.invalid/export.zip?signature=test'});
    assert.equal(request(7, values).send().skipped, false);
    const download = collection.item.find(row => row.name.startsWith('7.'));
    assert.equal(download.request.auth.type, 'noauth');
    assert.deepEqual(download.request.header, []);
    assert.equal(download.protocolProfileBehavior.disableCookies, true);
    values.job_id = '457';
    const stale = request(7, values);
    assert.throws(stale.send, /Check export/);
    assert.equal(stale.skipped, true);
});
