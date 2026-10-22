// Jenkins alternative to .github/workflows/ci-cd.yml: same stages, same scripts.
//
// Agent requirements: Linux (or WSL2) with Docker Engine, Docker Compose v2.23+,
// bash, git and curl. Plugins: Pipeline, Git, JUnit, Credentials Binding,
// Timestamper (all included in Jenkins' "suggested plugins").
//
// Optional publishing: set a global environment variable REGISTRY_IMAGE
// (for example docker.io/yourname/catalog-api) and add a username/password
// credential with the id "registry-credentials".

pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  environment {
    RUN_ID     = "${env.BUILD_NUMBER}"
    APP_IMAGE  = "catalog-api:jenkins-${env.BUILD_NUMBER}"
    TEST_IMAGE = "catalog-api-tests:jenkins-${env.BUILD_NUMBER}"
    // Jenkins itself usually listens on 8080, so the deployed app uses 8090.
    APP_PORT   = '8090'
  }

  stages {
    stage('Lint & Unit Tests') {
      steps {
        sh 'bash scripts/pipeline.sh unit'
      }
      post {
        always {
          junit allowEmptyResults: true, testResults: 'reports/unit-junit.xml'
        }
      }
    }

    stage('Build Image') {
      steps {
        sh 'APP_VERSION="$(git rev-parse HEAD)" bash scripts/pipeline.sh build'
      }
    }

    stage('Resilience Tests') {
      steps {
        sh 'bash scripts/pipeline.sh resilience'
      }
      post {
        always {
          junit allowEmptyResults: true, testResults: 'reports/resilience-junit.xml'
          archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
      }
    }

    stage('Publish Image') {
      when {
        allOf {
          expression { isMainBranch() }
          expression { (env.REGISTRY_IMAGE ?: '').trim().length() > 0 }
        }
      }
      steps {
        withCredentials([usernamePassword(credentialsId: 'registry-credentials',
                                          usernameVariable: 'REG_USER',
                                          passwordVariable: 'REG_PASS')]) {
          sh '''
            set -eu
            tag="sha-$(git rev-parse --short=7 HEAD)"
            echo "$REG_PASS" | docker login "${REGISTRY_IMAGE%%/*}" -u "$REG_USER" --password-stdin
            docker tag "$APP_IMAGE" "$REGISTRY_IMAGE:$tag"
            docker push "$REGISTRY_IMAGE:$tag"
          '''
        }
      }
    }

    stage('Deploy') {
      when {
        expression { isMainBranch() }
      }
      steps {
        sh '''
          set -eu
          image="$APP_IMAGE"
          if [ -n "${REGISTRY_IMAGE:-}" ]; then
            image="$REGISTRY_IMAGE:sha-$(git rev-parse --short=7 HEAD)"
          fi
          APP_IMAGE="$image" EXPECTED_VERSION="$(git rev-parse HEAD)" bash scripts/deploy.sh
        '''
      }
    }
  }
}

// Works for multibranch jobs (BRANCH_NAME=main) and plain pipeline jobs
// (GIT_BRANCH=origin/main).
def isMainBranch() {
  def branch = env.BRANCH_NAME ?: env.GIT_BRANCH ?: ''
  return branch.replaceFirst(/^origin\//, '') == 'main'
}
